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

`--tier2 shortlist` (`AuditorConfig.tier2`) changes two verdicts. Tier 2's
`mutation` is decided by `gates.check_mutation_shortlist` on the surviving
changed-line mutants, not on the kill rate, and an open survivor is
`not-proven`; tier 1's failing `coverage` becomes `not-proven` too, so tier 2
runs. `not-proven` is reported and does not refuse. `Findings.survivors`
lists every open survivor with its line, mutation and enclosing function.

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
import os
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
from halter.evidence import MutationOutcome, ruff_argv, ruff_findings, run_capture
from halter.gates import (
    DEFAULT_MUTANT_SHORTLIST,
    GateCheck,
    RuffFinding,
    check_mutation_shortlist,
    check_ruff,
    check_syntax,
    introduced_findings,
    set_aside_kind,
    shortlist_order,
)

Verdict = Literal["pass", "fail", "not-applicable", "blocked", "not-proven"]
Tier2Mode = Literal["score", "shortlist"]
TIER2_MODES: Final[tuple[Tier2Mode, ...]] = ("score", "shortlist")
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
class Survivor:
    """One surviving changed-line mutant, as a shortlist names it."""

    path: str
    line: int
    name: str
    status: str
    mutation: str
    """The mutant's removed and added lines (`evidence.mutation_text`)."""
    source: str
    """The changed line's own text, stripped."""
    behaviour: str
    """What the line serves: the enclosing function's name and docstring's first line."""


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
    survivors: tuple[Survivor, ...] = ()
    """`--tier2 shortlist` only: the tier-2 mutation finding's open survivors,
    all of them, in shortlist order (its detail names the first few). Empty,
    and absent from `to_dict`, otherwise."""
    mutant_detail: tuple[tuple[str, str, str], ...] = ()
    """Tier 2 only: (name, status, show) for every scored mutant
    (`MutationOutcome.mutant_detail`); absent from `to_dict` when empty.
    Recording only: kept in the verdict cache, left out of `--json`."""

    @property
    def passed(self) -> bool:
        return all(f.verdict in ("pass", "not-applicable", "not-proven") for f in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "key": self.key,
            "passed": self.passed,
            "cached": self.cached,
            "findings": [dataclasses.asdict(f) for f in self.findings],
            **(
                {"survivors": [dataclasses.asdict(v) for v in self.survivors]}
                if self.survivors
                else {}
            ),
            **(
                {
                    "mutant_detail": [
                        {"name": n, "status": s, "show": t} for n, s, t in self.mutant_detail
                    ]
                }
                if self.mutant_detail
                else {}
            ),
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Findings:
        raw = data["findings"]
        assert isinstance(raw, list)
        return Findings(
            tier=int(str(data["tier"])),
            key=str(data["key"]),
            findings=tuple(Finding(**{**f, "cites": tuple(f["cites"])}) for f in raw),
            survivors=tuple(Survivor(**v) for v in data.get("survivors", ())),  # type: ignore[attr-defined]
            mutant_detail=tuple(
                (d["name"], d["status"], d["show"])
                for d in data.get("mutant_detail", ())  # type: ignore[attr-defined]
            ),
        )


@dataclass(frozen=True)
class AuditorConfig:
    test_command: str = AUDIT_TEST_COMMAND
    cache_dir: Path | None = None
    extra_import_roots: tuple[str, ...] = field(default=("src",))
    tier2: Tier2Mode = "score"
    """`--tier2`: "score" (default) is the kill-rate verdict, byte for byte as
    before. "shortlist" decides tier 2 on open survivors
    (`gates.check_mutation_shortlist`) and makes coverage a locator: an
    uncovered changed line or an open survivor is `not-proven`, which is
    reported and does not refuse."""
    mutant_shortlist: int = DEFAULT_MUTANT_SHORTLIST
    """How many open survivors a mutation finding's detail names (`--mutant-shortlist`)."""


def _reason(gate: str, verdict: Verdict, detail: str) -> Reason:
    if verdict == "blocked" or (gate == "mutation" and detail.startswith(_TOOL_FAILURE_PREFIXES)):
        return "unknown"
    return REASONS[gate]


def _finding(gate: str, tier: int, verdict: Verdict, detail: str, basis: str | None) -> Finding:
    cites = (REUSES[gate],) if basis is None else (REUSES[gate], basis)
    return Finding(gate, tier, verdict, _reason(gate, verdict, detail), detail, cites)


def _from_check(check: GateCheck, tier: int) -> Finding:
    return _finding(check.name, tier, "pass" if check.passed else "fail", check.detail, check.basis)


def _rooted(outcome: MutationOutcome, copy: Path) -> MutationOutcome:
    """`outcome` with every survivor path relative to the audited tree's root."""

    def rel(path: str) -> str:
        return os.path.relpath(path, copy) if os.path.isabs(path) else path

    return dataclasses.replace(
        outcome,
        survivor_details=tuple(
            (name, status, rel(path), line, text, message)
            for name, status, path, line, text, message in outcome.survivor_details
        ),
    )


def _sources(copy: Path, outcome: MutationOutcome) -> dict[str, str]:
    """The text of every file a survivor sits in, read from the audited copy."""
    found: dict[str, str] = {}
    for _, _, path, _, _, _ in outcome.survivor_details:
        target = copy / path
        if path not in found and target.is_file():
            found[path] = target.read_text()
    return found


def behaviour_at(source: str, line: int) -> str:
    """The innermost def containing `line`: its name and its docstring's first line."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "module top level"
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and (best is None or node.lineno > best.lineno)
        ):
            best = node
    if best is None:
        return "module top level"
    doc = ast.get_docstring(best)
    first = doc.strip().splitlines()[0] if doc and doc.strip() else ""
    return f"`{best.name}`" + (f": {first}" if first else "")


def _survivors(outcome: MutationOutcome, sources: dict[str, str]) -> tuple[Survivor, ...]:
    found = []
    judged = [d for d in outcome.survivor_details if not d[5] and set_aside_kind(d) is None]
    for name, status, path, line, text, _ in shortlist_order(judged):
        source = sources.get(path, "")
        lines = source.splitlines()
        found.append(
            Survivor(
                path=path,
                line=line,
                name=name,
                status=status,
                mutation=text,
                source=lines[line - 1].strip() if 0 < line <= len(lines) else "",
                behaviour=behaviour_at(source, line),
            )
        )
    return tuple(found)


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
            [
                tier,
                *parts,
                self.config.test_command,
                self.node.model_dump_json(),
                gate_surface(),
                *(
                    ["shortlist", self.config.mutant_shortlist]
                    if self.config.tier2 == "shortlist"
                    else []
                ),
            ]
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
            if self.config.tier2 == "shortlist" and statuses.get("coverage", ("",))[0] == "fail":
                # Coverage is a locator under the shortlist: an uncovered changed
                # line is "not proven", never a refusal; the detail keeps its lines.
                statuses["coverage"] = ("not-proven", *statuses["coverage"][1:])
            survivors: tuple[Survivor, ...] = ()
            scored = gated.mutation.mutant_detail if gated.mutation is not None else ()
            cites: dict[str, str] = {}
            if self.config.tier2 == "shortlist" and tier == 2 and gated.mutation is not None:
                outcome = _rooted(gated.mutation, copy)
                sources = _sources(copy, outcome)
                shortlisted = check_mutation_shortlist(
                    outcome,
                    self.node.deterministic_gate.mutation_sample.kill_threshold,
                    shortlist=self.config.mutant_shortlist,
                    sources=sources,
                )
                # An open survivor is surfaced as "not proven", never a refusal.
                statuses["mutation"] = (
                    "pass" if shortlisted.passed else "not-proven",
                    shortlisted.detail,
                    shortlisted.basis,
                )
                cites["mutation"] = "halter.gates.check_mutation_shortlist"
                if not shortlisted.passed:
                    survivors = _survivors(outcome, sources)
        wanted = TIER1 if tier == 1 else TIER2
        findings = []
        for gate in wanted:
            status, detail, basis = statuses["tests" if gate == "full-suite" else gate]
            found = _finding(gate, tier, status, detail, basis)  # type: ignore[arg-type]
            if gate in cites:
                found = dataclasses.replace(found, cites=(cites[gate], *found.cites[1:]))
            findings.append(found)
        return self._store(
            Findings(
                tier=tier,
                key=key,
                findings=tuple(findings),
                survivors=survivors,
                mutant_detail=scored if tier == 2 else (),
            )
        )

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
