"""Tests for halter.auditor: the tiered battery and its tree-keyed cache.

Ported from saddle's tests/test_auditor.py (phase2-check 4a4b60d); the two
saddle tests of options `halter --tiered` never sets (a journal, a declared
plan node) are replaced by `test_without_a_declaration_scope_checks_are_not_applicable`.
Fixtures follow test_audit.py: the changed source line is `    return 2`, which
the conftest's autouse `mutmut` stub reports five killed mutants on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from halter import auditor as auditor_mod
from halter import runner
from halter.audit import NOT_APPLICABLE, AuditError, audit_node
from halter.auditor import (
    REASONS,
    REUSES,
    TIER0,
    TIER1,
    TIER2,
    Auditor,
    AuditorConfig,
    Findings,
    check_imports,
)
from halter.evidence import run_capture

BASE_CODE = "def f():\n    return 1\n"
FIXED_CODE = "def f():\n    return 2\n"
TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"

# The thirteen gates of `gates.run_tier1`, the denominator every tier split
# must account for exactly once (`full-suite` reuses `tests`' predicate).
THIRTEEN = {
    "syntax",
    "ruff",
    "tests",
    "coverage",
    "dead-code",
    "public-deletions",
    "red-phase",
    "node-scope",
    "target-scope",
    "property-coverage",
    "assertion-preservation",
    "requirement-binding",
    "mutation",
}


def _git(root: Path, *argv: str) -> None:
    assert run_capture(["git", *argv], root).exit_code == 0


def _init(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    for name, text in files.items():
        (root / name).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline")


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    return tree


@pytest.fixture
def uncovered_tree(clean_tree: Path) -> Path:
    (clean_tree / "m.py").write_text("def g():\n    return 7\n")
    return clean_tree


class _Spy:
    """Counts calls through to the wrapped function."""

    def __init__(self, target):  # type: ignore[no-untyped-def]
        self.target = target
        self.calls = 0

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.target(*args, **kwargs)


@pytest.fixture
def gate_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy(runner.run_node_gate)
    monkeypatch.setattr(runner, "run_node_gate", spy)
    return spy


@pytest.fixture
def mutation_spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy(runner.mutation_sample)
    monkeypatch.setattr(runner, "mutation_sample", spy)
    return spy


def _verdicts(result: Findings) -> dict[str, str]:
    return {f.gate: f.verdict for f in result.findings}


def test_tiers_partition_the_thirteen_gates() -> None:
    gates = set(TIER0) - {"imports"} | set(TIER1) | set(TIER2) - {"full-suite"}
    assert gates == THIRTEEN
    assert len(TIER0) + len(TIER1) + len(TIER2) == 15
    assert set(REUSES) == set(REASONS) == set(TIER0) | set(TIER1) | set(TIER2)
    assert set(REASONS.values()) <= {"code-wrong", "evidence-thin", "scope", "unknown"}


# -- known-good --------------------------------------------------------------


def test_correct_change_passes_every_tier_then_hits_the_cache(
    clean_tree: Path, gate_spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor = Auditor(clean_tree)
    results = auditor.audit()
    assert [r.tier for r in results] == [0, 0, 1, 2]
    for result in results:
        assert result.passed, result.findings
        assert not result.cached
    assert [f.gate for f in results[2].findings] == list(TIER1)
    assert [f.gate for f in results[3].findings] == list(TIER2)
    assert _verdicts(results[2])["node-scope"] == "not-applicable"
    mutation = next(f for f in results[3].findings if f.gate == "mutation")
    assert mutation.verdict == "pass"
    assert mutation.cites == ("halter.gates.check_mutation", "sampled n=5")
    # tier 1 once, tier 2 once (its tier-1 prerequisite is a cache hit).
    assert gate_spy.calls == 2
    syntax_spy = _Spy(auditor_mod.check_syntax)
    monkeypatch.setattr(auditor_mod, "check_syntax", syntax_spy)
    again = auditor.audit()
    assert all(r.cached for r in again)
    assert gate_spy.calls == 2
    assert syntax_spy.calls == 0
    assert [r.findings for r in again] == [r.findings for r in results]


def test_disk_cache_serves_a_new_auditor(clean_tree: Path, gate_spy: _Spy, tmp_path: Path) -> None:
    config = AuditorConfig(cache_dir=tmp_path / "cache")
    first = Auditor(clean_tree, config=config).tier1()
    second = Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 1
    assert second.cached
    assert second.findings == first.findings
    # A corrupt or mismatched file is a miss, never a verdict.
    (tmp_path / "cache" / f"{first.key}.json").write_text("{not json")
    Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 2
    other = json.loads((tmp_path / "cache" / f"{first.key}.json").read_text())
    other["key"] = "0" * 64
    (tmp_path / "cache" / f"{first.key}.json").write_text(json.dumps(other))
    Auditor(clean_tree, config=config).tier1()
    assert gate_spy.calls == 3


def test_without_a_declaration_scope_checks_are_not_applicable(clean_tree: Path) -> None:
    results = Auditor(clean_tree).audit()
    by_gate = {f.gate: f for r in results for f in r.findings}
    for name, reason in NOT_APPLICABLE.items():
        assert by_gate[name].verdict == "not-applicable"
        assert by_gate[name].detail == reason


def test_tier1_skips_the_tier2_evidence_legs(clean_tree: Path, mutation_spy: _Spy) -> None:
    Auditor(clean_tree).tier1()
    assert mutation_spy.calls == 0


# -- known-bad ---------------------------------------------------------------


def test_uncovered_changed_line_fails_tier1_and_blocks_tier2(
    uncovered_tree: Path, gate_spy: _Spy, mutation_spy: _Spy
) -> None:
    auditor = Auditor(uncovered_tree)
    first = auditor.tier1()
    assert _verdicts(first)["coverage"] == "fail"
    coverage = next(f for f in first.findings if f.gate == "coverage")
    assert coverage.reason == "evidence-thin"
    assert "m.py" in coverage.detail
    second = auditor.tier2()
    assert [(f.verdict, f.reason) for f in second.findings] == [("blocked", "unknown")]
    assert "coverage" in second.findings[0].detail
    assert mutation_spy.calls == 0
    assert gate_spy.calls == 1


def test_tier2_alone_runs_tier1_first(uncovered_tree: Path, mutation_spy: _Spy) -> None:
    result = Auditor(uncovered_tree).tier2()
    assert result.findings[0].verdict == "blocked"
    assert mutation_spy.calls == 0


def _untested_mutmut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub whose five mutants on n.py:2 no test runs."""
    stub = tmp_path / "untested-stub"
    stub.mkdir()
    results = "\n".join(f"  m{i}: no tests" for i in range(1, 6))
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script = stub / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")


def test_new_tests_that_kill_no_mutant_fail_tier2_counting_untested(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _untested_mutmut(tmp_path, monkeypatch)
    auditor = Auditor(clean_tree)
    assert auditor.tier1().passed
    result = auditor.tier2()
    mutation = next(f for f in result.findings if f.gate == "mutation")
    assert mutation.verdict == "fail"
    assert mutation.reason == "evidence-thin"
    assert "killed 0 of 5" in mutation.detail
    assert "5 untested" in mutation.detail
    assert not result.passed


def test_syntax_breaking_edit_fails_tier0(clean_tree: Path) -> None:
    result = Auditor(clean_tree).tier0("n.py", "def f(:\n    return 2\n")
    assert _verdicts(result) == {"syntax": "fail", "ruff": "fail", "imports": "pass"}
    syntax = result.findings[0]
    assert syntax.reason == "code-wrong"
    assert syntax.cites == ("halter.gates.check_syntax",)
    assert "not checked" in result.findings[2].detail


def test_tier0_ruff_charges_only_introduced_findings(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": "import os\n\n\ndef f():\n    return 1\n"})
    auditor = Auditor(tree)
    inherited = auditor.tier0("n.py", "import os\n\n\ndef f():\n    return 2\n")
    assert _verdicts(inherited)["ruff"] == "pass"
    assert "inherited: 1" in inherited.findings[1].detail
    introduced = auditor.tier0("m.py", "import os\n")
    assert _verdicts(introduced)["ruff"] == "fail"
    assert "F401" in introduced.findings[1].detail


def test_tier0_import_resolution(clean_tree: Path) -> None:
    auditor = Auditor(clean_tree)
    good = auditor.tier0("test_x.py", "import json\nimport pytest\nfrom n import f\n")
    assert _verdicts(good)["imports"] == "pass"
    bad = auditor.tier0("test_y.py", "import no_such_module_xyz\n\nno_such_module_xyz.f()\n")
    assert _verdicts(bad)["imports"] == "fail"
    assert "no_such_module_xyz" in bad.findings[2].detail


def test_check_imports_counts_relative_imports(tmp_path: Path) -> None:
    check = check_imports("p/a.py", "from . import b\nfrom .c import d\n", [tmp_path])
    assert check.passed
    assert "2 relative import(s) not checked" in check.detail
    missing = check_imports("a.py", "from . import b\nimport zz_missing\n", [tmp_path])
    assert not missing.passed
    assert "1 relative" in missing.detail
    (tmp_path / "pkg").mkdir()
    assert check_imports("a.py", "import pkg.sub\n", [tmp_path]).passed


def test_one_byte_change_misses_the_cache(clean_tree: Path, gate_spy: _Spy) -> None:
    auditor = Auditor(clean_tree)
    first = auditor.tier1()
    (clean_tree / "n.py").write_text(FIXED_CODE.replace("2", "3"))
    (clean_tree / "test_n.py").write_text(TEST_BODY.format(value=3))
    second = auditor.tier1()
    assert gate_spy.calls == 2
    assert first.key != second.key
    assert not second.cached
    zero_a = auditor.tier0("n.py", FIXED_CODE)
    zero_b = auditor.tier0("n.py", FIXED_CODE + " ")
    assert zero_a.key != zero_b.key
    assert not zero_b.cached


def test_an_unchanged_tree_is_an_audit_error(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    with pytest.raises(AuditError, match="nothing to audit"):
        Auditor(tree).tier1()


def test_tool_failure_is_reason_unknown() -> None:
    finding = auditor_mod._finding(
        "mutation", 2, "fail", "mutation tool failed: mutmut run exited 1", "sampled n=0"
    )
    assert finding.reason == "unknown"
    assert auditor_mod._finding("mutation", 2, "fail", "killed 0 of 5", None).reason == (
        "evidence-thin"
    )


def test_findings_round_trip() -> None:
    result = Findings(
        tier=1,
        key="k",
        findings=(auditor_mod._finding("tests", 1, "pass", "ok", None),),
    )
    assert Findings.from_dict(json.loads(json.dumps(result.to_dict()))) == result


def test_runner_tier2_false_measures_no_mutation(clean_tree: Path, mutation_spy: _Spy) -> None:
    _git(clean_tree, "add", "-A")
    gated = runner.run_node_gate(audit_node(), clean_tree, tier2=False)
    assert mutation_spy.calls == 0
    assert gated.mutation is not None
    assert gated.mutation.survivors == (runner.NOT_MEASURED_AT_TIER1,)
    full = runner.run_node_gate(audit_node(), clean_tree)
    assert mutation_spy.calls == 1
    assert full.mutation is not None
    assert full.mutation.killed == 5
