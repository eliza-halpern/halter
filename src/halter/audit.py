"""Audit a tree against a baseline commit and return one verdict.

`audit_tree(tree, baseline)` runs the check battery over the diff between
`baseline` and `tree` and returns one verdict: `nothing-to-audit` when the
staged tree equals the baseline's tree, `accept` when every applicable check
passed, `refuse` otherwise. The tree is taken as it is on disk: untracked
files count (a new module is the least-tested code in a tree, and `git diff`
never shows it) and ignored noise does not. The audit works on a copy, so the
audited tree -- its files and its `.git/index` alike -- is never written to.

Four checks need a declaration the audit does not have: `node-scope`,
`target-scope`, `requirement-binding` and `property-coverage` each judge a
change against something declared about it (which files it may touch, which
requirements it binds, what kind of change it is). A bare diff declares
nothing, so they are reported `not-applicable`, never `pass`, since a `pass`
would be a claim nothing measured. The remaining checks run exactly as
`runner.run_node_gate` runs them, on the synthesized `refactor` node
`audit_node` builds: the kind whose coverage check has full force.

With `cache=<dir>`, `audit_tree` serves a stored `AuditResult` only when the
staged tree, the resolved baseline, `test_command` and the gate surface
(`gate_surface()`: a hash of the check modules' bytes plus the shelled-out
tools' versions) all match a stored key exactly. Any other file state -- a
missing file, unparseable JSON, a mismatched key -- is a miss, never a verdict
and never an exception. `nothing-to-audit` is never cached.

Layering: this module imports `dag`, `evidence`, `journal` and `runner`;
only `cli` imports it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from halter.dag import Node
from halter.evidence import MutationOutcome, run_capture
from halter.journal import SpanRecorder
from halter.runner import run_node_gate

AUDIT_TEST_COMMAND: Final = "python -m pytest -q"

# The command line expands and uses this; `audit_tree` itself never defaults to it.
DEFAULT_AUDIT_CACHE: Final = Path("~/.cache/halter/audit")

# The modules whose bytes decide a verdict, and the tools the checks shell out
# to, in the order `gate_surface()` hashes them.
SURFACE_MODULES: Final[tuple[str, ...]] = (
    "halter.audit",
    "halter.runner",
    "halter.gates",
    "halter.evidence",
    "halter.dag",
)
SURFACE_TOOLS: Final[tuple[str, ...]] = ("mutmut", "ruff", "coverage")

# Tool noise a test or mutation run leaves behind, ignored at any depth: none
# of it is a change, so none of it may reach `git add -A` in the copy,
# whatever the audited tree's own `.gitignore`.
_COPY_IGNORE_ANY_DEPTH: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache", ".mutmut-cache"}
)
# Noise that halter, coverage and mutmut write only at the tree's own top
# level: a name here must not eat a tracked file of the same name deeper in
# the tree (`pkg/mutants/__init__.py`), and the `.coverage.` prefix rule in
# `_audit_ignore` must not eat a same-prefixed project file (`.coveragerc`,
# a project's own coverage config, is not `.coverage.*`).
_COPY_IGNORE_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {".halter", "mutants", ".coverage"}
)

# Check name -> the reason printed as its detail. Each of these checks judges
# the change against a declaration a bare diff does not carry.
NOT_APPLICABLE: Final[Mapping[str, str]] = {
    "node-scope": "not applicable: halter audits a bare diff, no change kind is declared",
    "target-scope": "not applicable: halter audits a bare diff, no target files are declared",
    "requirement-binding": "not applicable: halter audits a bare diff, no requirement is declared",
    "property-coverage": "not applicable: halter audits a bare diff, no property test is declared",
}


class AuditError(RuntimeError):
    """An unresolvable baseline, or a tree that is not a git repository."""


@dataclass(frozen=True)
class AuditCheck:
    """One check's outcome: its name, `pass`/`fail`/`not-applicable`, and the detail line."""

    name: str
    status: Literal["pass", "fail", "not-applicable"]
    detail: str
    basis: str | None


@dataclass(frozen=True)
class AuditResult:
    """Everything one audit decided, in the shape `--json` prints and the cache stores."""

    verdict: Literal["accept", "refuse", "nothing-to-audit"]
    tree: str  # `git write-tree` of the staged copy
    baseline: str  # the full 40-hex commit id `baseline` resolved to
    test_command: str
    checks: tuple[AuditCheck, ...]  # in `run_tier1`'s order; () for nothing-to-audit
    mutation: MutationOutcome | None  # None for nothing-to-audit
    surface: str  # gate_surface() at the time this result was produced
    cached: bool = False  # True when served from a stored key match, not gated

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view; `mutation` goes through `dataclasses.asdict`."""
        return {
            "verdict": self.verdict,
            "tree": self.tree,
            "baseline": self.baseline,
            "test_command": self.test_command,
            "checks": [asdict(check) for check in self.checks],
            "mutation": asdict(self.mutation) if self.mutation is not None else None,
            "surface": self.surface,
            "cached": self.cached,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> AuditResult:
        """The exact inverse of `to_dict`: every JSON list becomes a tuple, recursively."""
        mutation_data = data["mutation"]
        mutation = MutationOutcome(**_tuplify(mutation_data)) if mutation_data is not None else None
        return AuditResult(
            verdict=data["verdict"],
            tree=data["tree"],
            baseline=data["baseline"],
            test_command=data["test_command"],
            checks=tuple(AuditCheck(**check) for check in data["checks"]),
            mutation=mutation,
            surface=data["surface"],
            cached=data["cached"],
        )


def _tuplify(value: Any) -> Any:
    """Recursively turn every JSON list into a tuple: `asdict`'s exact inverse for tuples."""
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuplify(item) for key, item in value.items()}
    return value


def audit_node(test_command: str = AUDIT_TEST_COMMAND) -> Node:
    """The declaration the checks run under: a `refactor` change, every threshold at its ceiling."""
    return Node.model_validate(
        {
            "id": "audit",
            "kind": "refactor",
            "dependencies": [],
            "task_prompt": "Audit the tree against its baseline.",
            "requirements": [
                {
                    "id": "REQ-000",
                    "statement": "REQ-000 holds.",
                    "accepts": ["0"],
                    "rejects": ["1"],
                }
            ],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": ["read_file", "write_file", "run_tests", "lint"],
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": test_command,
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {"scope": "changed-lines"},
            },
        }
    )


def _git(copy: Path, *argv: str) -> str:
    """Stdout of `git <argv>` run in `copy`; an `AuditError` when it exits nonzero."""
    run = run_capture(["git", *argv], copy)
    if run.exit_code != 0:
        msg = f"git {' '.join(argv)} failed: {run.stderr.strip()}"
        raise AuditError(msg)
    return run.stdout.strip()


def _audit_ignore(root: Path) -> Callable[[str, list[str]], set[str]]:
    """`shutil.copytree`'s `ignore` callable: some names are noise at any depth,
    others only at `root` -- where halter, coverage and mutmut actually write
    them. A rule scoped to the top level must never key off a *prefix* that a
    same-named top-level project file also has (`.coveragerc` starts with
    `.coverage` but is not run noise).

    A name is dropped only when it matches a noise rule **and** git does not
    track it: neither the path itself nor any path beneath it is in
    `git ls-files`. A tracked file is part of the baseline, and a copy without
    it reads as a deletion, so an unchanged tree that happens to track
    `__pycache__/*.pyc` would otherwise stop being `nothing-to-audit`.
    """
    root_str = os.fspath(root)
    listed = run_capture(["git", "ls-files", "-z"], root)
    if listed.exit_code != 0:
        msg = f"git ls-files failed: {listed.stderr.strip()}"
        raise AuditError(msg)
    tracked = {name for name in listed.stdout.split("\0") if name}
    kept = tracked | {
        str(parent)
        for name in tracked
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {name for name in names if name in _COPY_IGNORE_ANY_DEPTH or name.endswith(".pyc")}
        if directory == root_str:
            skip |= {
                name
                for name in names
                if name in _COPY_IGNORE_TOP_LEVEL or name.startswith(".coverage.")
            }
        below = os.path.relpath(directory, root_str)
        prefix = "" if below == "." else f"{below}/"
        return {name for name in skip if f"{prefix}{name}" not in kept}

    return ignore


def gate_surface(
    files: Sequence[Path] | None = None,
    versions: Mapping[str, str] | None = None,
) -> str:
    """A hex sha256 of what decides a verdict: check module bytes, tool versions,
    and the interpreter version. Two audits agree only when all three agree.

    With no arguments, hashes each module named in `SURFACE_MODULES`' `__file__`
    bytes in order, then `importlib.metadata.version(t)` for each tool named in
    `SURFACE_TOOLS`, then `platform.python_version()`. The arguments let a test
    pass explicit files and versions instead.
    """
    if files is None:
        # `__file__` is `str | None` in general (a frozen or namespace import has
        # none), but every real module on disk has one; `cast` records that
        # without adding a branch a test would have to exercise to cover.
        files = [
            Path(cast(str, importlib.import_module(name).__file__)) for name in SURFACE_MODULES
        ]
    if versions is None:
        versions = {tool: importlib.metadata.version(tool) for tool in SURFACE_TOOLS}
    digest = hashlib.sha256()
    for path in files:
        digest.update(Path(path).read_bytes())
    for tool, version in versions.items():
        digest.update(f"{tool}={version}\n".encode())
    digest.update(platform.python_version().encode())
    return digest.hexdigest()


def _cache_key(tree: str, baseline: str, test_command: str, surface: str) -> dict[str, str]:
    """The four values a stored result must match exactly to be served."""
    return {"tree": tree, "baseline": baseline, "test_command": test_command, "surface": surface}


def _cache_path(cache: Path, key: Mapping[str, str]) -> Path:
    """`<cache>/<sha256 of the key as sorted JSON>.json`."""
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return cache / f"{digest}.json"


def _cache_read(path: Path, key: Mapping[str, str]) -> AuditResult | None:
    """A stored result whose key matches `key` exactly; any other file state is
    a miss, never a verdict and never an exception."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("key") != key or "result" not in data:
        return None
    try:
        stored = AuditResult.from_dict(data["result"])
    except (KeyError, TypeError, ValueError):
        # A matching key over a result that cannot be rebuilt (a hand edit, a
        # partial write that still parses) is a miss like any other bad file.
        return None
    return dataclasses.replace(stored, cached=True)


def _cache_write(cache: Path, path: Path, key: Mapping[str, str], result: AuditResult) -> None:
    """Write `{"key": key, "result": ...}` atomically: a temp file, then `os.replace`."""
    cache.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key": key, "result": result.to_dict()}, sort_keys=True)
    handle, tmp_name = tempfile.mkstemp(dir=cache, prefix=".audit-tmp-")
    with os.fdopen(handle, "w") as tmp_file:
        tmp_file.write(payload)
    os.replace(tmp_name, path)


def _spelled_from_the_root(
    checks: tuple[AuditCheck, ...], mutation: MutationOutcome | None, copy: Path
) -> tuple[tuple[AuditCheck, ...], MutationOutcome | None]:
    """Rewrite every path the checks spelled inside `copy` relative to the tree's root.

    The checks run on a temporary copy and name files as `<copy>/accounts.py:84`,
    a directory deleted when the audit returns and named differently every
    run. No string of an `AuditResult` may name it: `detail` and `basis` lose
    the `<copy>/` prefix, and each `survivor_lines` path goes relative, so two
    audits of one tree are byte-identical and an editor can open what is named.
    """
    prefix = f"{copy}{os.sep}"
    checks = tuple(
        dataclasses.replace(
            check,
            detail=check.detail.replace(prefix, ""),
            basis=None if check.basis is None else check.basis.replace(prefix, ""),
        )
        for check in checks
    )
    if mutation is not None:
        mutation = dataclasses.replace(
            mutation,
            survivor_lines=tuple(
                (os.path.relpath(path, copy), line) for path, line in mutation.survivor_lines
            ),
        )
    return checks, mutation


def audit_tree(
    tree: Path,
    baseline: str = "HEAD",
    *,
    test_command: str = AUDIT_TEST_COMMAND,
    recorder: SpanRecorder | None = None,
    cache: Path | None = None,
) -> AuditResult:
    """Audit `tree`, as it is on disk, against `baseline`; `tree` is never written to.

    With `cache=None` every audit runs the checks. With a directory, a stored
    result is served only on a key hit (see the module docstring), with
    `cached=True`; `nothing-to-audit` is never cached. `recorder`, when given,
    receives one span per subprocess the checks run.
    """
    # A linked worktree's `.git` is a file naming a gitdir outside the tree: a
    # copy of it would still write to that original, and `git add -A` changes
    # the index even when no source byte moves.
    if not (tree / ".git").is_dir():
        msg = f"{tree} is not a git repository (no .git directory)"
        raise AuditError(msg)
    surface = gate_surface()
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "tree"
        shutil.copytree(tree, copy, ignore=_audit_ignore(tree))
        # Untracked files must be staged: `git diff <ref>` sees tracked files
        # only, so a new module would be invisible to every check.
        _git(copy, "add", "-A")
        try:
            resolved = _git(copy, "rev-parse", "--verify", f"{baseline}^{{commit}}")
        except AuditError as exc:
            msg = f"cannot resolve baseline {baseline!r}: {exc}"
            raise AuditError(msg) from exc
        staged = _git(copy, "write-tree")
        if staged == _git(copy, "rev-parse", f"{resolved}^{{tree}}"):
            return AuditResult(
                verdict="nothing-to-audit",
                tree=staged,
                baseline=resolved,
                test_command=test_command,
                checks=(),
                mutation=None,
                surface=surface,
                cached=False,
            )
        key = _cache_key(staged, resolved, test_command, surface)
        cache_file = _cache_path(cache, key) if cache is not None else None
        if cache_file is not None:
            hit = _cache_read(cache_file, key)
            if hit is not None:
                return hit
        gated = run_node_gate(audit_node(test_command), copy, baseline=resolved, recorder=recorder)
    checks = tuple(
        AuditCheck(
            name=check.name,
            status="not-applicable"
            if check.name in NOT_APPLICABLE
            else ("pass" if check.passed else "fail"),
            detail=NOT_APPLICABLE.get(check.name, check.detail),
            basis=check.basis,
        )
        for check in gated.checks
    )
    checks, mutation = _spelled_from_the_root(checks, gated.mutation, copy)
    result = AuditResult(
        verdict="refuse" if any(check.status == "fail" for check in checks) else "accept",
        tree=staged,
        baseline=resolved,
        test_command=test_command,
        checks=checks,
        mutation=mutation,
        surface=surface,
        cached=False,
    )
    if cache is not None and cache_file is not None:
        _cache_write(cache, cache_file, key, result)
    return result
