"""The `halter` command.

`halter [REV] [--repo R] [--baseline B] [--test-command C] [--json]
[--cache DIR | --no-cache]` audits one tree of a git repository against a
baseline commit and exits 0 (accept), 1 (refuse), 2 (could not audit) or
3 (nothing to audit). `run_audit` does the work; `build_parser` declares
the arguments; `main` joins the two.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import IO, Final

from halter import __version__, audit
from halter.audit import AuditError, AuditResult, audit_tree
from halter.evidence import run_capture

AUDIT_EXIT_CODES: Final = {"accept": 0, "refuse": 1, "nothing-to-audit": 3}
AUDIT_COULD_NOT_AUDIT: Final = 2
_AUDIT_STATUS_LABELS: Final = {"pass": "PASS", "fail": "FAIL", "not-applicable": "n/a"}


def _audit_git_or_raise(cwd: Path, *argv: str, what: str) -> None:
    """Run `git <argv>` in `cwd`; a nonzero exit becomes an `AuditError` prefixed with `what`."""
    run = run_capture(["git", *argv], cwd)
    if run.exit_code != 0:
        msg = f"{what}: {run.stderr.strip()}"
        raise AuditError(msg)


def _render_audit(result: AuditResult, stdout: IO[str]) -> None:
    """The text report: a header, one line per check, then the verdict."""
    freshness = "cached" if result.cached else "fresh"
    stdout.write(
        f"audit tree {result.tree[:12]} baseline {result.baseline[:12]} "
        f"surface {result.surface[:12]} {freshness}\n"
    )
    width = max((len(check.name) for check in result.checks), default=0)
    for check in result.checks:
        label = _AUDIT_STATUS_LABELS[check.status]
        stdout.write(f"{label:<4} {check.name:<{width}}  {check.detail}\n")
    stdout.write(f"verdict: {result.verdict.replace('-', ' ')}\n")


def run_audit(args: argparse.Namespace, *, stdout: IO[str], stderr: IO[str]) -> int:
    """Audit what the arguments name, print the report and return the exit code.

    No revision: `--repo`'s working tree, untracked files included, against
    `--baseline` (default HEAD). A revision: that commit's tree, taken from a
    fresh clone so the source repository's uncommitted files cannot reach the
    checks and nothing is written to it. An `AuditError` (no repository,
    unknown revision or baseline) prints `error: ...` on stderr and returns
    `AUDIT_COULD_NOT_AUDIT`.
    """
    cache = None if args.no_cache else Path(args.cache).expanduser()
    rev: str | None = args.rev
    try:
        if rev is None:
            result = audit_tree(
                Path(args.repo),
                args.baseline or "HEAD",
                test_command=args.test_command,
                cache=cache,
            )
        else:
            with tempfile.TemporaryDirectory() as scratch:
                clone = Path(scratch) / "tree"
                _audit_git_or_raise(
                    Path(scratch),
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    str(Path(args.repo).resolve()),
                    str(clone),
                    what=f"cannot clone {args.repo}",
                )
                _audit_git_or_raise(
                    clone,
                    "checkout",
                    "--quiet",
                    "--detach",
                    rev,
                    what=f"cannot check out revision {rev!r}",
                )
                result = audit_tree(
                    clone,
                    args.baseline or f"{rev}^",
                    test_command=args.test_command,
                    cache=cache,
                )
    except AuditError as exc:
        print(f"error: {exc}", file=stderr)
        return AUDIT_COULD_NOT_AUDIT
    if args.json:
        stdout.write(json.dumps(result.to_dict(), indent=2) + "\n")
    else:
        _render_audit(result, stdout)
    return AUDIT_EXIT_CODES[result.verdict]


def build_parser() -> argparse.ArgumentParser:
    """The argument parser: an optional REV plus the options `run_audit` reads."""
    parser = argparse.ArgumentParser(
        prog="halter",
        description=(
            "Audit a change to a Python tree: run its tests and a battery of checks over "
            "the diff against a baseline commit, then exit 0 accept, 1 refuse, "
            "2 could not audit, 3 nothing to audit."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "rev",
        nargs="?",
        help="Commit to gate (default: --repo's working tree, untracked files included).",
    )
    parser.add_argument("--repo", default=".", help="Git repository to audit.")
    parser.add_argument(
        "--baseline", help="Commit to gate against (default: HEAD, or REV^ with a REV)."
    )
    parser.add_argument(
        "--test-command",
        default=audit.AUDIT_TEST_COMMAND,
        help="Command that runs the tests.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the result as JSON and nothing else."
    )
    parser.add_argument(
        "--cache", default=audit.DEFAULT_AUDIT_CACHE, help="Verdict cache directory."
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Neither read nor write the verdict cache."
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Entry point: parse `argv` (or `sys.argv[1:]`) and run the audit."""
    args = build_parser().parse_args(argv)
    return run_audit(args, stdout=stdout or sys.stdout, stderr=stderr or sys.stderr)
