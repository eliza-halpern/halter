"""The tiered battery: the same checks, split by what each one needs to run.

`halter --tiered` runs three tiers over the diff:

- **tier 0, per changed file** (`Auditor.tier0(path, new_text)`): `syntax`
  (`gates.check_syntax`), `ruff` on the one file (`evidence.ruff_findings`
  on the new text and on the baseline's copy, `gates.introduced_findings`,
  `gates.check_ruff`) and `imports` (`check_imports` below, the one check
  that is not one of the thirteen in `gates.run_tier1`). No suite runs.
- **tier 1** (`Auditor.tier1(tree)`): `tests`, `coverage`, `dead-code`,
  `public-deletions`, `node-scope`, `target-scope` and
  `assertion-preservation`, read off one `runner.run_node_gate(...,
  tier2=False)` run, which skips the mutation run, the property oracle and
  all but one red-phase sample.
- **tier 2** (`Auditor.tier2(tree)`): `mutation` (untested mutants counted
  as survivors), `property-coverage`, `red-phase`, `requirement-binding`
  and `full-suite` (the `tests` check of the same full `runner.run_node_gate`
  run). Tier 2 never runs on a tree whose tier 1 failed: it returns one
  `blocked` finding naming the tier-1 checks that failed instead.

Every verdict is keyed by the tree (`audit.staged_copy`'s `git write-tree`
over the working tree with untracked files staged) plus the resolved
baseline, the test command, the declaration and `audit.gate_surface()`, so an
identical tree is never checked twice at the same tier. Tier 0 is keyed by
the file's path and bytes instead, since it sees one file and no tree.

A bare diff declares nothing, so the four declaration-relative checks are
`not-applicable` here exactly as in `audit.audit_tree` (`audit.NOT_APPLICABLE`).

`imports` asks halter's own interpreter (`importlib.util.find_spec`), not the
one the tree's test command runs under: a third-party name installed only in
the tree's environment is reported unresolved.

Layering: imports `audit`, `evidence`, `gates` and `runner`; only `cli`
imports it.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from halter import runner
from halter.audit import (
    AUDIT_TEST_COMMAND,
    AuditError,
    audit_checks,
    audit_node,
    baseline_tree,
    gate_surface,
    staged_copy,
)
from halter.evidence import ruff_argv, ruff_findings, run_capture
from halter.gates import GateCheck, RuffFinding, check_ruff, check_syntax, introduced_findings

Verdict = Literal["pass", "fail", "not-applicable", "blocked"]
Reason = Literal["code-wrong", "evidence-thin", "scope", "unknown"]

# Which check runs at which tier; `Auditor` emits them in exactly this order.
TIER0: Final[tuple[str, ...]] = ("syntax", "ruff", "imports")
TIER1: Final[tuple[str, ...]] = (
    "tests",
    "coverage",
    "dead-code",
    "public-deletions",
    "node-scope",
    "target-scope",
    "assertion-preservation",
)
TIER2: Final[tuple[str, ...]] = (
    "mutation",
    "property-coverage",
    "red-phase",
    "requirement-binding",
    "full-suite",
)

# The function each finding's verdict comes from (the `cites` of a finding).
REUSES: Final[dict[str, str]] = {
    "syntax": "halter.gates.check_syntax",
    "ruff": "halter.gates.check_ruff",
    "imports": "halter.auditor.check_imports",
    "tests": "halter.gates.check_test_command",
    "coverage": "halter.gates.check_changed_line_coverage",
    "dead-code": "halter.gates.check_dead_additions",
    "public-deletions": "halter.gates.check_public_deletions",
    "node-scope": "halter.gates.check_node_scope",
    "target-scope": "halter.gates.check_target_files",
    "assertion-preservation": "halter.gates.check_assertion_preservation",
    "mutation": "halter.gates.check_mutation",
    "property-coverage": "halter.gates.check_property_coverage",
    "red-phase": "halter.gates.check_red_phase",
    "requirement-binding": "halter.gates.check_requirement_binding",
    "full-suite": "halter.gates.check_test_command",
}

# What a failure of each check claims about the change: that the code is
# wrong, that the evidence for it is thin, or that the change strayed.
REASONS: Final[dict[str, Reason]] = {
    "syntax": "code-wrong",
    "ruff": "code-wrong",
    "imports": "code-wrong",
    "tests": "code-wrong",
    "public-deletions": "code-wrong",
    "full-suite": "code-wrong",
    "coverage": "evidence-thin",
    "dead-code": "evidence-thin",
    "assertion-preservation": "evidence-thin",
    "mutation": "evidence-thin",
    "property-coverage": "evidence-thin",
    "red-phase": "evidence-thin",
    "requirement-binding": "evidence-thin",
    "node-scope": "scope",
    "target-scope": "scope",
}

# A mutation detail that names a tool that never decided anything is no
# claim about the code or its tests (`gates.check_mutation`'s own wording).
_TOOL_FAILURE_PREFIXES: Final = ("mutation tool failed", "mutation not measured")


@dataclass(frozen=True)
class Finding:
    """One check's verdict at one tier."""

    gate: str
    tier: int
    verdict: Verdict
    reason: Reason
    detail: str
    cites: tuple[str, ...]


@dataclass(frozen=True)
class Findings:
    """A tier's findings over one tree (or, at tier 0, one file's bytes)."""

    tier: int
    key: str
    findings: tuple[Finding, ...]
    cached: bool = False

    @property
    def passed(self) -> bool:
        return all(f.verdict in ("pass", "not-applicable") for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "key": self.key,
            "passed": self.passed,
            "cached": self.cached,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Findings:
        raw = data["findings"]
        assert isinstance(raw, list)
        return Findings(
            tier=int(str(data["tier"])),
            key=str(data["key"]),
            findings=tuple(Finding(**{**f, "cites": tuple(f["cites"])}) for f in raw),
        )


@dataclass(frozen=True)
class AuditorConfig:
    test_command: str = AUDIT_TEST_COMMAND
    cache_dir: Path | None = None
    extra_import_roots: tuple[str, ...] = field(default=("src",))


def _reason(gate: str, verdict: Verdict, detail: str) -> Reason:
    if verdict == "blocked" or (gate == "mutation" and detail.startswith(_TOOL_FAILURE_PREFIXES)):
        return "unknown"
    return REASONS[gate]


def _finding(gate: str, tier: int, verdict: Verdict, detail: str, basis: str | None) -> Finding:
    cites = (REUSES[gate],) if basis is None else (REUSES[gate], basis)
    return Finding(gate, tier, verdict, _reason(gate, verdict, detail), detail, cites)


def _from_check(check: GateCheck, tier: int) -> Finding:
    return _finding(check.name, tier, "pass" if check.passed else "fail", check.detail, check.basis)


def check_imports(path: str, source: str, roots: Sequence[Path]) -> GateCheck:
    """Every absolute import's top-level name resolves: stdlib, a module or
    package under one of `roots`, or an installed distribution.

    Only top-level names are looked up, with `importlib.util.find_spec`,
    which imports nothing for a top-level name; relative imports are not
    checked and are counted in the detail. The interpreter asked is halter's
    own, not necessarily the one the tree's tests run under.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return GateCheck(name="imports", passed=True, detail="not checked: file does not parse")
    names: list[str] = []
    relative = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative += 1
            else:
                names.append(str(node.module).split(".")[0])
    missing = sorted(
        {
            name
            for name in names
            if name not in sys.stdlib_module_names
            and not any((r / f"{name}.py").is_file() or (r / name).is_dir() for r in roots)
            and importlib.util.find_spec(name) is None
        }
    )
    note = f"; {relative} relative import(s) not checked" if relative else ""
    if missing:
        return GateCheck(
            name="imports",
            passed=False,
            detail=f"{path}: unresolved import(s): {', '.join(missing)}{note}",
        )
    return GateCheck(
        name="imports", passed=True, detail=f"{len(set(names))} name(s) resolved{note}"
    )


class Auditor:
    """The tiered battery over `repo` against `baseline_rev`, with a verdict cache."""

    def __init__(self, repo: Path, baseline_rev: str = "HEAD", config: AuditorConfig | None = None):
        self.repo = Path(repo)
        self.baseline_rev = baseline_rev
        self.config = config or AuditorConfig()
        self.node = audit_node(self.config.test_command)
        self._memory: dict[str, Findings] = {}

    # -- cache ---------------------------------------------------------------

    def _key(self, tier: int, *parts: str) -> str:
        payload = json.dumps(
            [tier, *parts, self.config.test_command, self.node.model_dump_json(), gate_surface()]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _cached(self, key: str) -> Findings | None:
        hit = self._memory.get(key)
        if hit is None and self.config.cache_dir is not None:
            try:
                data = json.loads((self.config.cache_dir / f"{key}.json").read_text())
                hit = Findings.from_dict(data)
            except (OSError, ValueError, KeyError, TypeError, AssertionError):
                hit = None
        if hit is None or hit.key != key:
            return None
        return dataclasses.replace(hit, cached=True)

    def _store(self, result: Findings) -> Findings:
        self._memory[result.key] = result
        if self.config.cache_dir is not None:
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.config.cache_dir / f"{result.key}.json").write_text(
                json.dumps(result.to_dict(), sort_keys=True)
            )
        return result

    # -- tier 0 --------------------------------------------------------------

    def tier0(self, path: str, new_text: str) -> Findings:
        """Syntax, ruff and import resolution over one file's proposed text."""
        rel = PurePosixPath(path).as_posix()
        key = self._key(0, self.baseline_rev, rel, hashlib.sha256(new_text.encode()).hexdigest())
        hit = self._cached(key)
        if hit is not None:
            return hit
        syntax = check_syntax({rel: new_text})
        with tempfile.TemporaryDirectory(prefix="halter-tier0-") as tmp:
            current = Path(tmp) / "current"
            before = Path(tmp) / "baseline"
            (current / rel).parent.mkdir(parents=True)
            (current / rel).write_text(new_text)
            lint, found = ruff_findings(current, [rel])
            fmt = run_capture(ruff_argv("format", "--check", rel), current)
            shown = run_capture(["git", "show", f"{self.baseline_rev}:{rel}"], self.repo)
            old: list[RuffFinding] = []
            if shown.exit_code == 0:
                (before / rel).parent.mkdir(parents=True)
                (before / rel).write_text(shown.stdout)
                old = ruff_findings(before, [rel])[1]
        introduced, inherited = introduced_findings(found, old)
        ruff = check_ruff(
            [rel],
            introduced=tuple(introduced),
            inherited=inherited,
            lint_exit=lint.exit_code,
            format_exit=fmt.exit_code,
        )
        roots = [
            self.repo,
            (self.repo / rel).parent,
            *(self.repo / r for r in self.config.extra_import_roots),
        ]
        imports = check_imports(rel, new_text, roots)
        findings = tuple(_from_check(c, 0) for c in (syntax, ruff, imports))
        return self._store(Findings(tier=0, key=key, findings=findings))

    # -- tiers 1 and 2 -------------------------------------------------------

    def _gate(self, tier: int, tree: Path | None) -> Findings:
        with staged_copy(tree or self.repo, self.baseline_rev) as (copy, staged, resolved):
            if staged == baseline_tree(copy, resolved):
                msg = f"nothing to audit: the tree equals baseline {resolved[:12]}"
                raise AuditError(msg)
            key = self._key(tier, staged, resolved)
            hit = self._cached(key)
            if hit is not None:
                return hit
            if tier == 2:
                first = self.tier1(copy)
                if not first.passed:
                    failed = ", ".join(f.gate for f in first.findings if f.verdict == "fail")
                    blocked = _finding(
                        "mutation", 2, "blocked", f"tier 1 failed ({failed}); tier 2 not run", None
                    )
                    return self._store(Findings(tier=2, key=key, findings=(blocked,)))
            gated = runner.run_node_gate(self.node, copy, baseline=resolved, tier2=tier == 2)
            checks, _ = audit_checks(gated.checks, gated.mutation, copy)
            statuses = {c.name: (c.status, c.detail, c.basis) for c in checks}
        wanted = TIER1 if tier == 1 else TIER2
        findings = []
        for gate in wanted:
            status, detail, basis = statuses["tests" if gate == "full-suite" else gate]
            findings.append(_finding(gate, tier, status, detail, basis))  # type: ignore[arg-type]
        return self._store(Findings(tier=tier, key=key, findings=tuple(findings)))

    def tier1(self, tree: Path | None = None) -> Findings:
        """Tier 1 over `tree` (default: the repo's working tree)."""
        return self._gate(1, tree)

    def tier2(self, tree: Path | None = None) -> Findings:
        """Tier 2; `blocked` when tier 1 on the same tree fails."""
        return self._gate(2, tree)

    def audit(self, tree: Path | None = None) -> tuple[Findings, ...]:
        """Tiers 0-2 over the diff: tier 0 on every changed Python file's text."""
        root = tree or self.repo
        with staged_copy(root, self.baseline_rev) as (copy, _staged, resolved):
            names = run_capture(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=AMR", resolved], copy
            ).stdout.split()
            texts = {n: (copy / n).read_text() for n in names if n.endswith(".py")}
        zero = [self.tier0(n, t) for n, t in sorted(texts.items())]
        return (*zero, self.tier1(root), self.tier2(root))
