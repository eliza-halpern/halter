"""A span journal: one line per subprocess an audit ran.

`audit_tree(..., recorder=SpanRecorder(path, node_id))` appends one
`SpanRecord` per `git`, `ruff`, `coverage`, `mutmut` and test-command
invocation to `path` as JSONL, fsync'd on write: the argv (secret-shaped
values redacted), a hash of it, the exit code, the duration, the start
time and the first 500 characters of stderr. Each line carries a sha256
over its own canonical JSON, so a reader can tell an edited line from a
sealed one. Nothing in halter reads a journal back; it exists for the
caller who asked for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict


class SpanRecord(BaseModel):
    """One completed subprocess, as sealed into the journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["span"] = "span"
    span_id: str
    parent_id: str | None = None
    kind: Literal["tool", "agent"] = "tool"
    node_id: str
    name: str
    argv: list[str]
    args_hash: str
    duration_ms: int
    exit_code: int
    detail: str
    # UTC ISO-8601 start of the span, so journal lines can be ordered and
    # joined to other logs by wall clock. Empty when the writer gave none.
    started_at: str = ""
    # Reserved for a hash of a file beside the journal describing an
    # `agent`-kind span. Always empty for the `tool` spans halter writes.
    attempt_hash: str = ""
    record_hash: str


MAX_THINKING_CHARS: Final = 4000
STAR: Final = "***"
_KEY_PATTERN: Final = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_AWS_PATTERN: Final = re.compile(r"AKIA[0-9A-Z]{16}")
_NAMED_PATTERN: Final = re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*([^\s,;]+)")


def _scrub_bearer(text: str) -> str:
    """Replace bearer token values, keeping the scheme word for context."""
    return re.sub(r"(Bearer)\s+[A-Za-z0-9_.~+/-]+", r"\1 " + STAR, text)


def redact_secrets(text: str) -> str:
    """Redact secret-shaped spans. Length is NOT preserved: the named-key
    rule replaces its whole match, separator and value, with `name=***`."""
    scrubbed = _KEY_PATTERN.sub(STAR, text)
    scrubbed = _AWS_PATTERN.sub(STAR, scrubbed)
    scrubbed = _NAMED_PATTERN.sub(r"\1=" + STAR, scrubbed)
    return _scrub_bearer(scrubbed)


def scrub_thinking(text: str) -> str:
    """Redact secret-shaped spans, then cap length with a truncation marker."""
    scrubbed = redact_secrets(text)
    if len(scrubbed) > MAX_THINKING_CHARS:
        over = len(scrubbed) - MAX_THINKING_CHARS
        scrubbed = scrubbed[:MAX_THINKING_CHARS] + f"\n[truncated {over} chars]"
    return scrubbed


def _canonical_hash(payload: dict[str, Any]) -> str:
    """sha256 over canonical JSON: sorted keys, compact separators."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


MAX_SPAN_DETAIL_CHARS: Final = 500


def _tool_name(argv: list[str]) -> str:
    """Basename of the invoked tool, or "?" when argv is empty."""
    if not argv:
        return "?"
    first = argv[0]
    return first[first.rfind("/") + 1 :]


def build_span(
    *,
    node_id: str,
    argv: Sequence[str],
    duration_ms: int,
    exit_code: int,
    detail: str,
    kind: Literal["tool", "agent"] = "tool",
    name: str | None = None,
    parent_id: str | None = None,
    span_id: str | None = None,
    attempt_hash: str = "",
    started_at: str = "",
) -> SpanRecord:
    """Seal one span: scrubbed argv, hashed args, capped detail, start time."""
    scrubbed = [scrub_thinking(part) for part in argv]
    encoded_args = json.dumps(scrubbed).encode()
    payload: dict[str, Any] = {
        "record_type": "span",
        "span_id": span_id if span_id is not None else uuid.uuid4().hex,
        "parent_id": parent_id,
        "kind": kind,
        "node_id": node_id,
        "name": name if name is not None else _tool_name(scrubbed),
        "argv": scrubbed,
        "args_hash": hashlib.sha256(encoded_args).hexdigest(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "detail": scrub_thinking(detail)[:MAX_SPAN_DETAIL_CHARS],
    }
    if started_at:
        payload["started_at"] = started_at
    if attempt_hash:
        payload["attempt_hash"] = attempt_hash
    return SpanRecord.model_validate({**payload, "record_hash": _canonical_hash(payload)})


def _append_line(path: Path, line: str) -> None:
    """Append one line; fsync before returning so kill -9 keeps it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((line + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())


def append_span(path: Path, span: SpanRecord) -> None:
    """Append one tool-span line."""
    _append_line(path, json.dumps(span.model_dump(exclude_unset=True), sort_keys=True))


def utc_now() -> datetime:
    """The current UTC time; `SpanRecorder`'s default clock."""
    return datetime.now(UTC)


def started_before(duration_ms: int, now: datetime) -> str:
    """The UTC ISO-8601 instant `duration_ms` before `now`: when a span that just ended began."""
    return (now - timedelta(milliseconds=duration_ms)).isoformat()


@dataclass(frozen=True)
class SpanRecorder:
    """Journal sink for one audit's subprocess spans: `path` receives one line per `record`."""

    path: Path
    node_id: str
    parent_id: str | None = None
    # Wall clock for `started_at`; injectable so a test can pin it.
    clock: Callable[[], datetime] = utc_now

    def record(
        self,
        *,
        argv: Sequence[str],
        duration_ms: int,
        exit_code: int,
        detail: str,
        name: str | None = None,
    ) -> None:
        """Seal and append one completed tool invocation.

        `name` overrides the tool name derived from `argv`, for a run whose
        purpose the journal should show rather than its executable.
        """
        append_span(
            self.path,
            build_span(
                node_id=self.node_id,
                argv=list(argv),
                duration_ms=duration_ms,
                exit_code=exit_code,
                detail=detail,
                name=name,
                parent_id=self.parent_id,
                started_at=started_before(duration_ms, self.clock()),
            ),
        )
