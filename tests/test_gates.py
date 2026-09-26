"""Tests for halter.gates: one killer fixture per Tier-1 check."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from halter.dag import DeterministicGate, Node
from halter.evidence import MutationOutcome
from halter.gates import (
    PYTEST_COLLECTION_ERROR,
    PYTEST_TESTS_FAILED,
    RED_PHASE_SAMPLES,
    RUFF_NAMED_FINDINGS,
    SHELL_TIMEOUT,
    TOOL_UNAVAILABLE,
    GateCheck,
    RuffFinding,
    Tier1Inputs,
    check_assertion_preservation,
    check_changed_line_coverage,
    check_dead_additions,
    check_mutation,
    check_node_scope,
    check_property_coverage,
    check_public_deletions,
    check_red_phase,
    check_requirement_binding,
    check_ruff,
    check_syntax,
    check_target_files,
    check_test_command,
    compelled_lines,
)

# Every tool name the global allowlist carries (T3-4). A node listing all
# four behaves exactly as it did before each name was bound to a harness
# behaviour, so this is the fixtures' known-good default; a test that pins
# one binding passes a shorter list.
ALL_TOOLS: Final[tuple[str, ...]] = ("read_file", "write_file", "run_tests", "lint")


def _node(tools: list[str] | None = None, kind: str = "refactor") -> Node:
    return Node.model_validate(
        {
            "id": "n1",
            "kind": kind,
            "dependencies": [],
            "task_prompt": "Do n1.",
            "requirements": [
                {"id": "REQ-001", "statement": "REQ-001 holds.", "accepts": ["2"], "rejects": ["3"]}
            ],
            "execution_constraints": {
                "reasoning_budget": "low",
                "allowed_tools": tools if tools is not None else list(ALL_TOOLS),
                "max_context_tokens": 8000,
            },
            "deterministic_gate": {
                "test_command": "pytest tests/test_n1.py",
                "changed_line_coverage_min": 100.0,
                "red_phase_required": True,
                "mutation_sample": {
                    "scope": "changed-lines",
                    "max_mutants": 100,
                    "kill_threshold": 85.0,
                },
            },
        }
    )


def _passing_inputs() -> Tier1Inputs:
    return Tier1Inputs(
        sources={"n1.py": "x = 1\n"},
        ruff_files=["n1.py"],
        ruff_introduced=(),
        ruff_inherited=0,
        ruff_lint_exit=0,
        ruff_format_exit=0,
        test_runner=lambda _cmd: 0,
        changed={("n1.py", 1)},
        covered={("n1.py", 1)},
        baseline_exits=(1,) * RED_PHASE_SAMPLES,
        baseline_output="",
        baseline_tests={},
        tests_changed=True,
        current_runner=lambda: 0,
        flipped_tests={"test_a": "def test_a():  # REQ-001\n    assert True\n"},
        mutation=MutationOutcome(killed=9, total=10, generated=10, survivors=("m1",)),
        added_lines={},
        dead_code_runner=lambda _edited: 0,
        baseline_sources={"n1.py": "x = 0\n"},
    )


def test_syntax_killer_fixture_invalid_syntax_fails() -> None:
    check = check_syntax({"node.py": "def broken(:\n    pass\n"})
    assert check.passed is False
    assert check.name == "syntax"
    assert "node.py" in check.detail


def _finding(
    code: str = "F401", path: str = "n.py", row: int = 1, line: str = "import os"
) -> RuffFinding:
    return RuffFinding(code=code, path=path, row=row, message=f"{code} message", line=line)


def test_ruff_introduced_finding_fails_and_is_named() -> None:
    """T6-3 known-bad: a finding the baseline did not carry fails the node,
    and the detail names rule, file and line (F21.13d: `ruff check exited
    1` told the worker nothing)."""
    check = check_ruff(["n.py"], introduced=[_finding()], inherited=0, lint_exit=1, format_exit=0)
    assert check.passed is False
    assert check.name == "ruff"
    assert check.detail == "introduced 1 finding(s): n.py:1 F401 F401 message"


def test_ruff_inherited_finding_is_reported_and_does_not_fail() -> None:
    """T6-3 known-good: a finding the baseline already carried is inherited;
    the node passes with it counted in the detail."""
    check = check_ruff(["n.py"], introduced=[], inherited=1, lint_exit=1, format_exit=0)
    assert check.passed is True
    assert check.detail == "1 file(s) clean; inherited: 1"


def test_ruff_format_failure_fails() -> None:
    check = check_ruff(["n.py"], introduced=[], inherited=0, lint_exit=0, format_exit=2)
    assert check.passed is False
    assert check.detail == "ruff format --check exited 2"


def test_ruff_nonzero_with_nothing_parsed_fails_closed() -> None:
    """A nonzero `ruff check` that yielded no findings and inherited none is
    the tool failing, not a clean tree."""
    check = check_ruff(["n.py"], introduced=[], inherited=0, lint_exit=2, format_exit=0)
    assert check.passed is False
    assert check.detail == "ruff check exited 2 with no findings parsed"


def test_ruff_clean_passes() -> None:
    check = check_ruff(["b.py", "a.py"], introduced=[], inherited=0, lint_exit=0, format_exit=0)
    assert check.passed is True
    assert check.detail == "2 file(s) clean"


def test_ruff_detail_elides_past_the_named_findings() -> None:
    many = [_finding(row=i, line=f"line {i}") for i in range(1, RUFF_NAMED_FINDINGS + 3)]
    check = check_ruff(["n.py"], introduced=many, inherited=2, lint_exit=1, format_exit=0)
    assert check.detail.endswith("(+2 more); inherited: 2")
    assert check.detail.count("F401 message") == RUFF_NAMED_FINDINGS


def test_ruff_no_files_passes_without_running() -> None:
    check = check_ruff([], introduced=[], inherited=0, lint_exit=0, format_exit=0)
    assert check.passed is True
    assert check.name == "ruff"
    assert check.detail == "no files to lint"


def test_syntax_valid_sources_pass() -> None:
    check = check_syntax({"node.py": "def ok():\n    return 1\n"})
    assert check.passed is True
    assert check.detail == "1 file(s) parsed"


def test_tests_killer_fixture_failing_command_fails() -> None:
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: 1)
    assert check.passed is False
    assert check.name == "tests"
    assert "exited 1" in check.detail


def test_tests_passing_command_passes() -> None:
    seen: list[str] = []

    def run(cmd: str) -> int:
        seen.append(cmd)
        return 0

    check = check_test_command("pytest tests/test_x.py", run)
    assert check.passed is True
    assert check.detail == "'pytest tests/test_x.py' exited 0"
    assert seen == ["pytest tests/test_x.py"]


def test_coverage_killer_fixture_uncovered_line_fails() -> None:
    check = check_changed_line_coverage({("node.py", 3), ("node.py", 4)}, set(), 100.0)
    assert check.passed is False
    assert check.name == "coverage"
    assert check.detail == "no test runs node.py:3, node.py:4"
    assert check.basis == "changed-lines=2"


def test_coverage_full_cover_and_empty_diff_pass() -> None:
    full = check_changed_line_coverage({("node.py", 3)}, {("node.py", 3)}, 100.0)
    assert full.passed is True
    assert full.name == "coverage"
    assert full.detail == "every changed line runs"
    assert full.basis == "changed-lines=1"
    empty = check_changed_line_coverage(set(), set(), 100.0)
    assert empty.passed is True
    assert empty.name == "coverage"
    assert empty.detail == "no changed lines"
    assert empty.basis == "changed-lines=0"
    # Checks whose detail already says everything carry no basis.
    assert check_syntax({"n.py": "x = 1\n"}).basis is None


def test_coverage_detail_names_the_lines_and_carries_no_ratio() -> None:
    """T6-44: `detail` is routed to the worker, so it states behaviour.

    Known-bad: `0.0% < 100.0%: uncovered node.py:3` reaching the worker.
    A percentage is satisfiable by a call that runs the line and asserts
    nothing, which is what round 3e's n2 attempt 1 wrote sixty times.
    The lines are the evidence; the ratio is a number to optimise. The
    ratio stays in `basis`, which is sealed and not worker-facing.
    """
    check = check_changed_line_coverage({("node.py", 3), ("node.py", 4)}, set(), 100.0)
    assert check.passed is False
    assert "%" not in check.detail
    assert check.detail == "no test runs node.py:3, node.py:4"
    assert check.basis == "changed-lines=2"

    partial = check_changed_line_coverage(
        {("n.py", 1), ("n.py", 2), ("n.py", 3)}, {("n.py", 1), ("n.py", 2)}, 67.0
    )
    assert partial.passed is False
    assert partial.detail == "no test runs n.py:3"

    full = check_changed_line_coverage({("node.py", 3)}, {("node.py", 3)}, 100.0)
    assert full.passed is True
    assert "%" not in full.detail


def test_mutation_detail_keeps_its_ratio_on_purpose() -> None:
    """T6-44 scope: the known-bad this item deliberately still admits.

    Coverage is satisfiable by a no-op and its ratio is removed. Mutation
    is not -- a `pass` body admits no mutant -- and round 3d's n2 went
    75.3% -> 78.3% by writing real tests against this very string. If a
    draw is ever seen inflating mutation score with mutable-but-
    meaningless lines, this assertion is the one that must change.
    """
    outcome = MutationOutcome(killed=1, total=7, generated=7, survivors=("s1", "s2"))
    check = check_mutation(outcome, 85.0)
    assert check.passed is False
    assert "%" in check.detail
    assert "survived 2: s1, s2" in check.detail


def test_coverage_partial_percent_compared_to_minimum() -> None:
    """The ratio still decides the verdict; T6-44 only took it out of `detail`.

    This used to assert the prefix `66.7% < 67.0%`, which pinned the
    wording rather than the comparison. Two thirds covered now has to
    fail at a minimum of 67.0 and pass at 66.0, so a mutation of the
    comparison is visible where a mutation of the message is not.
    """
    changed = {("n.py", 1), ("n.py", 2), ("n.py", 3)}
    covered = {("n.py", 1), ("n.py", 2)}
    check = check_changed_line_coverage(changed, covered, 67.0)
    assert check.passed is False
    assert check.detail == "no test runs n.py:3"
    assert check.basis == "changed-lines=3"
    just_under = check_changed_line_coverage(changed, covered, 66.0)
    assert just_under.passed is True
    assert just_under.detail == "every changed line runs"


_STRONG = MutationOutcome(killed=10, total=10, generated=10, survivors=())
_PASSING_COVERAGE = GateCheck(name="coverage", passed=True, detail="every changed line runs")


def _red(
    baseline: int,
    current: int,
    *,
    output: str = "",
    changed: tuple[str, ...] = (),
    tests_changed: bool = True,
    kind: str = "",
    coverage: GateCheck = _PASSING_COVERAGE,
    mutation: MutationOutcome = _STRONG,
) -> GateCheck:
    # Callers that predate the kind split describe themselves by whether
    # the node touched tests; a refactor is the no-test-change case. The
    # differential (fail pre-change, pass post-change) is the impl node's
    # path: a test node is a red specification with no baseline leg
    # (T3-7a), and these fixtures used to name it "test" because nothing
    # had yet tried to run one (T3-7).
    kind = kind or ("impl" if tests_changed else "refactor")
    return check_red_phase(
        (baseline,) * RED_PHASE_SAMPLES,
        lambda: current,
        baseline_output=output,
        changed_files=changed,
        tests_changed=tests_changed,
        kind=kind,
        coverage=coverage,
        mutation=mutation,
    )


def test_red_phase_killer_fixture_pass_pre_change_fails() -> None:
    check = _red(0, 0)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests pass pre-change; prove nothing"


def test_red_phase_fail_post_change_fails() -> None:
    check = _red(1, 1)
    assert check.passed is False
    assert check.name == "red-phase"
    assert check.detail == "tests fail post-change"


def test_red_phase_flip_passes() -> None:
    flip = _red(1, 0)
    assert flip.passed is True
    assert flip.name == "red-phase"
    assert flip.detail == "fail pre-change, pass post-change"


def test_red_phase_cannot_be_waived() -> None:
    """The waiver is gone from the signature and unrepresentable on the wire.

    `const: true` is what makes guided decoding unable to emit a waiver,
    so the planner cannot opt a node out of the tautology killer.
    """
    assert "required" not in inspect.signature(check_red_phase).parameters
    schema = DeterministicGate.model_json_schema()
    assert schema["properties"]["red_phase_required"]["const"] is True


def test_red_phase_unrelated_baseline_error_fails() -> None:
    """Exit 2 blaming something the node never touched proves nothing."""
    check = _red(2, 0, output="ImportError: No module named 'yaml'", changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline collection error names no changed source; prove nothing"


def test_red_phase_greenfield_import_error_is_red() -> None:
    """Exit 2 naming a changed module is the greenfield red case."""
    check = _red(2, 0, output="ModuleNotFoundError: No module named 'n'", changed=("/w/n.py",))
    assert check.passed is True
    assert check.detail == "fail pre-change, pass post-change"


def test_red_phase_collection_header_names_changed_file() -> None:
    check = _red(2, 0, output="ERROR collecting n.py", changed=("/w/n.py",))
    assert check.passed is True


def test_red_phase_missing_file_exit_proves_nothing() -> None:
    """Exit 4 (file not found) was the old vacuous-red path."""
    check = _red(4, 0, output="file or directory not found", changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline exit 4: tests never ran; prove nothing"


def test_red_phase_no_tests_collected_proves_nothing() -> None:
    check = _red(5, 0, changed=("/w/n.py",))
    assert check.passed is False
    assert check.detail == "baseline exit 5: tests never ran; prove nothing"


def test_binding_killer_fixture_unbound_requirement_fails() -> None:
    check = check_requirement_binding(
        ["REQ-001", "REQ-002"], {"test_a": "def test_a():\n    assert True\n"}
    )
    assert check.passed is False
    assert check.name == "requirement-binding"
    assert check.detail == "unbound requirements: REQ-001, REQ-002"


def test_binding_all_bound_passes() -> None:
    check = check_requirement_binding(
        ["REQ-001"], {"test_a": "def test_a():  # REQ-001\n    assert True\n"}
    )
    assert check.passed is True
    assert check.detail == "1 requirement(s) bound"


# --- T3-24: the orphan half subtracts the ids the rest of the plan declares --


def test_binding_citation_of_another_planned_id_is_not_an_orphan() -> None:
    """Known-good: the node declares REQ-002, the plan also holds REQ-001,
    and the suite cites both -- the shape of every test-first split with
    distinct ids (session 20b). The pass detail counts the node's own ids."""
    check = check_requirement_binding(
        ["REQ-002"],
        {"test_n.py": "def test_a():  # REQ-001\n    pass\n\n\ndef test_b():  # REQ-002\n"},
        planned_ids=("REQ-001", "REQ-002"),
    )
    assert check.passed is True
    assert check.detail == "1 requirement(s) bound"


def test_binding_citation_of_an_id_no_node_declares_is_still_an_orphan() -> None:
    """Known-bad: REQ-003 is declared by no node of the plan."""
    check = check_requirement_binding(
        ["REQ-002"],
        {"test_n.py": "# REQ-001\n# REQ-002\n# REQ-003\n"},
        planned_ids=("REQ-001", "REQ-002"),
    )
    assert check.passed is False
    assert check.detail == "undeclared requirements cited: REQ-003"


def test_binding_planned_ids_do_not_rescue_an_unbound_own_id() -> None:
    """The unbound half is per node: REQ-002 is planned and declared here
    but no test cites it, and the plan citing REQ-001 changes nothing."""
    check = check_requirement_binding(
        ["REQ-002"], {"test_n.py": "# REQ-001\n"}, planned_ids=("REQ-001", "REQ-002")
    )
    assert check.passed is False
    assert check.detail == "unbound requirements: REQ-002"


def test_binding_own_undeclared_citation_is_an_orphan_with_no_plan() -> None:
    """A node gated alone (empty plan) that cites an id it did not declare
    is still caught: the node's own ids are subtracted, not replaced."""
    check = check_requirement_binding(["REQ-002"], {"test_n.py": "# REQ-002\n# REQ-009\n"})
    assert check.passed is False
    assert check.detail == "undeclared requirements cited: REQ-009"


def test_mutation_below_threshold_fails_with_survivors() -> None:
    outcome = MutationOutcome(
        killed=1, total=7, generated=7, survivors=("s6", "s5", "s4", "s3", "s2", "s1")
    )
    check = check_mutation(outcome, 85.0)
    assert check.name == "mutation"
    assert check.passed is False
    assert check.detail == (
        "killed 1 of 7 changed-line mutants (14.3% < 85.0%): survived 6: s1, s2, s3, s4, s5, ..."
    )
    assert check.basis == "sampled n=7"


def test_mutation_boundary_threshold_passes() -> None:
    outcome = MutationOutcome(killed=17, total=20, generated=20, survivors=("s1",))
    check = check_mutation(outcome, 85.0)
    assert check.name == "mutation"
    assert check.passed is True
    assert check.detail == "killed 17 of 20 changed-line mutants (85.0% >= 85.0%)"
    # T2-4: the verdict carries its evidence basis, so a reader can tell a
    # pass over 20 mutants from a pass over 0 without parsing the detail.
    assert check.basis == "sampled n=20"


def test_mutation_fail_detail_states_the_denominator() -> None:
    """T6-97 (WORKPLAN, key pair 1). F21.70 had to rebuild n=29 by
    arithmetic from `82.8% < 85.0%: survived 5: ...` because the detail
    never printed how many mutants were decided; the count precedes the
    percentage so the worker's next attempt sees both."""
    outcome = MutationOutcome(
        killed=24, total=29, generated=29, survivors=("s1", "s2", "s3", "s4", "s5")
    )
    check = check_mutation(outcome, 85.0)
    assert check.passed is False
    assert check.detail.startswith(
        "killed 24 of 29 changed-line mutants (82.8% < 85.0%): survived 5: "
    )


def test_mutation_pass_detail_states_the_denominator() -> None:
    """T6-97 (WORKPLAN, key pair 2): the pass detail also names both
    counts, not just the percentage over the total."""
    outcome = MutationOutcome(killed=9, total=10, generated=10, survivors=("s1",))
    check = check_mutation(outcome, 85.0)
    assert check.passed is True
    assert check.detail == "killed 9 of 10 changed-line mutants (90.0% >= 85.0%)"


def test_mutation_no_longer_passes_without_mutants() -> None:
    """Was `test_mutation_vacuous_passes_without_mutants`, asserting
    `passed is True` on an empty sample. The name said vacuous and the
    assertion pinned it; T7 shipped an infinite loop through this branch
    (F12). Inverted rather than deleted so the history stays legible."""
    check = check_mutation(MutationOutcome(killed=0, total=0, generated=0, survivors=()), 85.0)
    assert check.name == "mutation"
    assert check.passed is False
    assert check.detail == "no mutants on changed lines: mutation provided no evidence"
    assert check.basis == "sampled n=0"


def test_mutation_undecided_fails_with_cause() -> None:
    bare = check_mutation(MutationOutcome(killed=0, total=0, generated=3, survivors=()), 85.0)
    assert bare.name == "mutation"
    assert bare.passed is False
    assert bare.detail == "no mutants decided"
    assert bare.basis == "sampled n=0"
    caused = check_mutation(
        MutationOutcome(
            killed=0, total=0, generated=0, survivors=("c6", "c5", "c4", "c3", "c2", "c1")
        ),
        85.0,
    )
    assert caused.name == "mutation"
    assert caused.passed is False
    assert caused.detail == "no mutants decided: c1, c2, c3, c4, c5"


def test_red_phase_behaviour_preserving_node_leans_on_coverage_and_mutation() -> None:
    """A node whose diff changes no test cannot have a pre-change failure."""
    check = _red(0, 0, tests_changed=False)
    assert check.passed is True
    assert check.detail == (
        "tests unchanged (behaviour preserved); coverage and mutation 100.0% carry the proof"
    )


def test_red_phase_behaviour_preserving_needs_coverage() -> None:
    failed = GateCheck(name="coverage", passed=False, detail="no test runs n.py:2")
    check = _red(0, 0, tests_changed=False, coverage=failed)
    assert check.passed is False
    assert check.detail == "tests unchanged and coverage failed; nothing proves the change"


def test_red_phase_behaviour_preserving_needs_decided_mutants() -> None:
    none_decided = MutationOutcome(killed=0, total=0, generated=0, survivors=())
    check = _red(0, 0, tests_changed=False, mutation=none_decided)
    assert check.passed is False
    assert check.detail == "tests unchanged and no mutants decided; nothing proves the change"


def test_red_phase_behaviour_preserving_uses_fixed_floor_not_node_threshold() -> None:
    """The floor is fixed so a planner-chosen kill_threshold cannot waive it."""
    weak = MutationOutcome(killed=1, total=10, generated=10, survivors=("m1",))
    check = _red(0, 0, tests_changed=False, mutation=weak)
    assert check.passed is False
    assert check.detail == "tests unchanged and mutation 10.0% < 85.0%; nothing proves the change"


def test_tests_hanging_command_reports_hang_not_exit_code() -> None:
    """A hang is a distinct defect class, not an ordinary nonzero exit.

    Operationally a hang is worse than a failure: harnesses that parse
    pytest counts get partial or zero output from a suite that never
    terminates. The transcript must say so rather than burying it as
    'exited 124'.
    """
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: SHELL_TIMEOUT)
    assert check.passed is False
    assert check.name == "tests"
    assert "hang" in check.detail.lower()
    assert "exited" not in check.detail


def test_red_phase_hanging_baseline_names_the_hang() -> None:
    """A baseline that never terminates is not 'tests never ran'.

    It fails the gate either way, but the transcript has to say which:
    exit 124 read as a generic nonzero exit sends recovery looking for a
    missing file instead of a loop.
    """
    check = check_red_phase(
        (SHELL_TIMEOUT,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orderedlist.py"],
        tests_changed=True,
        kind="impl",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is False
    assert "hang" in check.detail.lower()
    assert "never ran" not in check.detail


def test_red_phase_nondeterministic_baseline_proves_nothing() -> None:
    """A flaky baseline makes the differential meaningless.

    Red-phase is the only gate that reasons over *two* runs, so its
    evidence is exactly as good as the stability of the pre-change leg.
    A test that fails on one baseline run and passes on the next gives
    'fail pre-change, pass post-change' with no causal relation to the
    diff at all -- a vacuous red indistinguishable from a genuine one.
    2-16% of failures in large suites are flaky, and nothing in the
    harness would have noticed.
    """
    check = check_red_phase(
        (PYTEST_TESTS_FAILED, 0, PYTEST_TESTS_FAILED),
        lambda: 0,
        baseline_output="",
        changed_files=["n.py"],
        tests_changed=True,
        kind="impl",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is False
    assert "nondeterministic" in check.detail.lower()


def test_red_phase_unanimous_baseline_still_passes() -> None:
    """Sampling must not change the verdict on a stable baseline."""
    check = check_red_phase(
        (PYTEST_TESTS_FAILED,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["n.py"],
        tests_changed=True,
        kind="impl",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=1, total=1, killed=1, survivors=()),
    )
    assert check.passed is True


def test_tests_missing_tool_names_the_tool_not_an_exit_code() -> None:
    """`exited 127` sends recovery rewriting code over a broken venv."""
    check = check_test_command("pytest tests/test_x.py", lambda _cmd: TOOL_UNAVAILABLE)
    assert check.passed is False
    assert "unavailable" in check.detail.lower()
    assert "exited" not in check.detail


def test_ruff_missing_tool_names_the_tool() -> None:
    check = check_ruff(
        ["n.py"], introduced=[], inherited=0, lint_exit=TOOL_UNAVAILABLE, format_exit=0
    )
    assert check.passed is False
    assert "unavailable" in check.detail.lower()


def test_requirement_binding_rejects_ids_the_node_never_declared() -> None:
    """A REQ ID in a test that no node declares is a hallucinated one.

    traceSDD's orphan rule: every ID cited in code is a verifiable claim,
    and one absent from the spec is automatically detectable. Without it
    the binding gate is satisfiable in both directions -- the worker can
    tag whatever it likes, and F5's circularity survives the statements.
    """
    check = check_requirement_binding(
        ["REQ-001"],
        {"tests/test_n.py": "def test_a():  # REQ-001\n    pass\n\n# REQ-742: invented\n"},
    )
    assert check.passed is False
    assert "REQ-742" in check.detail
    assert "undeclared" in check.detail.lower()


def test_requirement_binding_needs_every_example_asserted_on() -> None:
    """T6-4: a cited id is not a tested example. Known-bad is T1's shape:
    the tests cite REQ-001 and probe no reject; the detail names each
    example nothing asserts on. Known-good asserts on both, one as a
    string in an `assert`, one in a `parametrize` table, one under
    `raises`; a non-string constant is matched by its spelling."""
    examples = [
        ("REQ-001", "accepts", "user@example.com"),
        ("REQ-001", "rejects", "user@@example.com"),
        ("REQ-001", "rejects", "user@example.com."),
    ]
    only_accepts = {
        "test_v.py": "def test_ok():  # REQ-001\n    assert valid('user@example.com')\n"
    }
    check = check_requirement_binding(["REQ-001"], only_accepts, examples=examples)
    assert check.passed is False
    assert check.detail == (
        "examples no test asserts on: REQ-001 rejects 'user@@example.com', "
        "REQ-001 rejects 'user@example.com.'"
    )
    every = {
        "test_v.py": (
            "import pytest\n\n\n"
            "def test_ok():  # REQ-001\n    assert valid('user@example.com')\n\n\n"
            "@pytest.mark.parametrize('bad', ['user@@example.com'])\n"
            "def test_bad(bad):  # REQ-001\n    assert not valid(bad)\n\n\n"
            "def test_trailing_dot():\n"
            "    with pytest.raises(ValueError, match='user@example.com.'):\n"
            "        parse('x')\n"
        )
    }
    check = check_requirement_binding(["REQ-001"], every, examples=examples)
    assert check.passed is True
    assert check.detail == "1 requirement(s) bound, 3 example(s) asserted"
    numeric = {"test_n.py": "def test_f():  # REQ-001\n    assert f() == 2\n"}
    assert check_requirement_binding(
        ["REQ-001"], numeric, examples=[("REQ-001", "accepts", "2")]
    ).passed
    assert not check_requirement_binding(
        ["REQ-001"], numeric, examples=[("REQ-001", "accepts", "3")]
    ).passed


def test_requirement_binding_binds_a_call_example_to_the_test_that_performs_it() -> None:
    """T6-50, from round 3f (F21.20): the planner writes examples as calls
    -- `deposit('10.00', 'USD')` -- and `_asserted_literals` collects
    constants, so under the literal rule alone no behavioural test could
    ever satisfy one. Round 3f died on exactly this: both `n1` and its
    replacement failed all three attempts on `requirement-binding` and
    nothing else, and the tree of `refs/halter/attempt/n1/3` calls
    `deposit`, asserts `10.00`, `USD` and `USX`, and was rejected anyway.

    Known-good: the test that performs the operation and asserts on the
    values it was handed binds it -- through a receiver, under `raises`
    for the reject, and in either quote style, because constants compare
    by value and `autofix` runs `ruff format` before the gate."""
    examples = [
        ("REQ-001", "accepts", "deposit('10.00', 'USD')"),
        ("REQ-001", "rejects", "deposit('10.00', 'USX')"),
        ("REQ-001", "accepts", "total_fees(5, 'USD')"),
    ]
    behavioural = {
        "test_a.py": (
            "import pytest\n\n\n"
            "def test_deposit_usd():  # REQ-001\n"
            '    assert Account("ann").deposit("10.00", "USD") == Decimal("10.00")\n\n\n'
            "def test_unknown_currency():\n"
            "    with pytest.raises(ValueError):\n"
            '        Account("ann").deposit("10.00", "USX")\n\n\n'
            "def test_fees():\n"
            '    assert total_fees(5, "USD") == Decimal("1.25")\n'
        )
    }
    check = check_requirement_binding(["REQ-001"], behavioural, examples=examples)
    assert check.passed is True
    assert check.detail == "1 requirement(s) bound, 3 example(s) asserted"

    keyword = {
        "test_a.py": (
            "def test_deposit():  # REQ-001\n    assert acc.deposit(amount, currency='USD') == 1\n"
        )
    }
    assert check_requirement_binding(
        ["REQ-001"], keyword, examples=[("REQ-001", "accepts", "deposit(amount, currency='USD')")]
    ).passed

    # The planner may name the receiver too; the operation is what binds.
    assert check_requirement_binding(
        ["REQ-001"], behavioural, examples=[("REQ-001", "accepts", "acc.deposit('10.00', 'USD')")]
    ).passed


def test_requirement_binding_refuses_a_call_example_that_is_quoted_not_performed() -> None:
    """The tightening half of T6-50. The literal rule's one satisfying
    source was a test that quotes the example and asserts nothing about
    the behaviour -- the shape the whole project exists to reject -- so
    closing it is the point, not a side effect.

    Known-bad, three ways, each naming the half that is missing: the
    example quoted as a string; the values asserted with the operation
    never performed; the operation performed with neither value
    asserted."""
    examples = [("REQ-001", "accepts", "deposit('10.00', 'USD')")]
    quoted = {
        "test_a.py": (
            "def test_cites():  # REQ-001\n"
            "    assert \"deposit('10.00', 'USD')\" == \"deposit('10.00', 'USD')\"\n"
        )
    }
    check = check_requirement_binding(["REQ-001"], quoted, examples=examples)
    assert check.passed is False
    assert check.detail == (
        "examples no test asserts on: REQ-001 accepts \"deposit('10.00', 'USD')\" "
        "(no test calls deposit)"
    )
    values_only = {"test_a.py": 'def test_v():  # REQ-001\n    assert "10.00" in ledger("USD")\n'}
    check = check_requirement_binding(["REQ-001"], values_only, examples=examples)
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit)")
    wrong_values = {
        "test_a.py": (
            'def test_c():  # REQ-001\n    acc.deposit("5.00", "USD")\n'
            '    assert "10.00" and "USD"\n'
        )
    }
    check = check_requirement_binding(["REQ-001"], wrong_values, examples=examples)
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit with '10.00', 'USD')")
    asserts_nothing = {"test_a.py": 'def test_c():  # REQ-001\n    acc.deposit("10.00", "USD")\n'}
    check = check_requirement_binding(["REQ-001"], asserts_nothing, examples=examples)
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit)")


def test_requirement_binding_binds_a_call_the_test_assigns_before_asserting() -> None:
    """Round 3j (F21.41): the binding proxy asked whether the example's
    constants appear inside an `assert`, which is not where a Python test
    puts the arguments of the call it is exercising. A reject is written
    `with pytest.raises(...): deposit("0.001", "USD")` -- a `With`, which
    `_asserted_literals` collects -- and an accept is written
    `result = deposit("1.001", "USD")` then `assert result == ...`, an
    `Assign`, which nothing collects. So the gate bound every reject and
    missed every accept: six attempts of round 3j, accepts 3 unbound and
    rejects 1 unbound, every time, with no variance at all.

    The proxy is replaced by the thing it stood for: a test must CALL the
    operation with the example's constant arguments. That is looser in
    one direction and tighter in the other -- the decoy below calls with
    different values while the example's constants sit in an assert
    elsewhere, and the proxy bound it.
    """
    example = [("REQ-001", "accepts", "deposit('1.001', 'USD')")]
    assign_then_assert = {
        "test_a.py": (
            "def test_sub_cent_rounds():  # REQ-001\n"
            '    result = acc.deposit("1.001", "USD")\n'
            '    assert result == Decimal("1.00")\n'
        )
    }
    assert check_requirement_binding(["REQ-001"], assign_then_assert, examples=example).passed
    decoy = {
        "test_a.py": (
            "def test_decoy():  # REQ-001\n"
            '    result = acc.deposit("5.00", "USD")\n'
            '    assert "1.001" not in str(result) and "USD"\n'
        )
    }
    check = check_requirement_binding(["REQ-001"], decoy, examples=example)
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit with '1.001', 'USD')")
    # The example names the salient arguments, not necessarily all of them,
    # so a call supplying MORE binds and a call supplying FEWER does not.
    # Nothing else pins the direction of the comparison: every other case
    # here has the two sets exactly equal, and a mutant reversing it lived.
    extra_arguments = {
        "test_a.py": (
            "def test_extra():  # REQ-001\n"
            '    result = acc.deposit("1.001", "USD", strict=True)\n'
            '    assert result == Decimal("1.00")\n'
        )
    }
    assert check_requirement_binding(["REQ-001"], extra_arguments, examples=example).passed
    missing_argument = {
        "test_a.py": (
            "def test_missing():  # REQ-001\n"
            '    result = acc.deposit("1.001")\n'
            '    assert result == Decimal("1.00")\n'
        )
    }
    check = check_requirement_binding(["REQ-001"], missing_argument, examples=example)
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit with '1.001', 'USD')")


def test_requirement_binding_keeps_the_literal_rule_for_what_is_not_a_call() -> None:
    """The call rule applies to examples that parse as a call and to
    nothing else, so T6-4's literal examples are untouched. A call the
    gate cannot name -- the callee is itself an expression -- falls back
    with them, and a call in a function that is not a test does not count
    as performing anything."""
    literal_ish = {"test_a.py": 'def test_a():  # REQ-001\n    assert f() == "10.00 USD"\n'}
    assert check_requirement_binding(
        ["REQ-001"], literal_ish, examples=[("REQ-001", "accepts", "10.00 USD")]
    ).passed
    assert not check_requirement_binding(
        ["REQ-001"], literal_ish, examples=[("REQ-001", "accepts", "10.00 EUR")]
    ).passed
    computed = {"test_a.py": 'def test_a():  # REQ-001\n    assert handler()("x")\n'}
    check = check_requirement_binding(
        ["REQ-001"], computed, examples=[("REQ-001", "accepts", 'handler()("x")')]
    )
    assert check.passed is False
    assert check.detail.endswith("REQ-001 accepts 'handler()(\"x\")'")
    helper_only = {
        "test_a.py": (
            "def helper():\n"
            '    acc.deposit("10.00", "USD")\n\n\n'
            "def test_a():  # REQ-001\n"
            '    assert "10.00" and "USD"\n'
        )
    }
    check = check_requirement_binding(
        ["REQ-001"], helper_only, examples=[("REQ-001", "accepts", "deposit('10.00', 'USD')")]
    )
    assert check.passed is False
    assert check.detail.endswith("(no test calls deposit)")


def test_requirement_binding_binds_a_data_example_to_the_values_inside_it() -> None:
    """Round 3j (F21.41): an example written as structured data -- the
    planner's version-2 store record -- fell to the literal rule, which
    binds only a test quoting the whole blob as one string. That inverted
    the gate: the suite that saved a ledger and asserted the written JSON
    equals the record was REFUSED, and a test that quoted the text and
    asserted nothing about behaviour PASSED. Four attempts across two
    nodes died on it with a byte-identical detail and no suite could have
    passed, because the shape T6-50 repaired was the call branch only.

    So a data example binds the way a call example does: by the constants
    inside it, spelled as `_asserted_literals` spells them. The accept and
    the reject differ by one constant (`2` against `3`), so one test does
    not bind both -- the property that makes the check worth running.
    """
    v2 = '{"version": 2, "accounts": [{"owner": "a", "balances": {"USD": "10.00"}}]}'
    v3 = '{"version": 3, "accounts": [{"owner": "a", "balances": {"USD": "10.00"}}]}'
    behavioural = {
        "test_store.py": (
            "import json\n\n\n"
            "def test_saved_schema(tmp_path):  # REQ-001\n"
            "    save_accounts([acc], path)\n"
            "    assert json.loads(path.read_text()) == {\n"
            '        "version": 2,\n'
            '        "accounts": [{"owner": "a", "balances": {"USD": "10.00"}}],\n'
            "    }\n"
        )
    }
    assert check_requirement_binding(
        ["REQ-001"], behavioural, examples=[("REQ-001", "accepts", v2)]
    ).passed
    check = check_requirement_binding(
        ["REQ-001"], behavioural, examples=[("REQ-001", "rejects", v3)]
    )
    assert check.passed is False
    assert check.detail.endswith("(no test asserts on '3')")
    # The known-bad this admits, exhibited (WORKPLAN 0.6): the values may
    # be asserted without the behaviour being exercised. The flat-literal
    # rule already admitted the same shape -- quoting the blob verbatim --
    # so this widens what counts as bound, never what counts as tested.
    values_only = {
        "test_store.py": (
            "def test_values():  # REQ-001\n"
            '    assert "version" and 2 and "accounts" and "owner"\n'
            '    assert "a" and "balances" and "USD" and "10.00"\n'
        )
    }
    assert check_requirement_binding(
        ["REQ-001"], values_only, examples=[("REQ-001", "accepts", v2)]
    ).passed


def test_requirement_binding_reads_examples_from_the_suite_it_is_given() -> None:
    """The literal may sit in a test the node did not write (a sealed spec
    node's, when a survivor round adds a file beside it): `suite` is what
    is searched, `flipped_tests` still carries the citations. A literal
    outside a test function, or outside an assert, does not count, and an
    unparseable file holds none."""
    flipped = {"test_more.py": "def test_more():  # REQ-001\n    assert f() == 2\n"}
    suite = {**flipped, "test_n.py": "def test_f():\n    assert not f() == 3\n"}
    examples = [("REQ-001", "rejects", "3")]
    assert check_requirement_binding(["REQ-001"], flipped, examples=examples).passed is False
    assert check_requirement_binding(["REQ-001"], flipped, examples=examples, suite=suite).passed
    stray = {
        "test_n.py": (
            "THREE = 3  # REQ-001\n\n\ndef helper():\n    assert 3\n\n\n"
            "def test_f():\n    x = 3\n    assert x\n"
        )
    }
    assert check_requirement_binding(["REQ-001"], stray, examples=examples).passed is False
    broken = {"test_n.py": "def test_f(:  # REQ-001\n    assert 3\n"}
    assert check_requirement_binding(["REQ-001"], broken, examples=examples).passed is False


def test_requirement_binding_passes_when_every_cited_id_is_declared() -> None:
    check = check_requirement_binding(
        ["REQ-001", "REQ-002"],
        {"tests/test_n.py": "# REQ-001\n# REQ-002\n"},
    )
    assert check.passed is True


def test_mutation_zero_mutants_no_longer_passes() -> None:
    """Zero mutants is zero evidence, not a clean bill of health.

    F12: T7's node added a 218-line module, produced no changed-line
    mutants, and the gate reported `PASS (no mutants on changed lines)`
    having tested nothing. An infinite loop shipped past it. The
    fail-open was deliberate -- a change with no mutable surface cannot
    be under-tested -- but the premise is false: mutmut only mutates
    function bodies, so a module-scope `_RE = re.compile(...)` yields 0
    mutants where the same expression inline yields 7.
    """
    outcome = MutationOutcome(generated=0, total=0, killed=0, survivors=())
    check = check_mutation(outcome, 85.0)
    assert check.passed is False
    assert "no mutants" in check.detail.lower()


def test_mutation_small_sample_demands_every_mutant() -> None:
    """A percentage over a tiny sample is noise, so take no partial credit.

    F1: T1's four-line regex admitted 2 mutants, two shallow tests killed
    both, and the gate read 100%. The validator still accepted
    `.u@example.com` and `user@example..com`. The threshold cannot fix
    that, but it can refuse to call 3-of-4 adequate.
    """
    small = MutationOutcome(generated=4, total=4, killed=3, survivors=("m4",))
    check = check_mutation(small, 85.0)
    assert check.passed is False
    assert "small sample" in check.detail.lower()

    large = MutationOutcome(generated=20, total=20, killed=18, survivors=("m1", "m2"))
    assert check_mutation(large, 85.0).passed is True


def test_mutation_detail_always_reports_the_sample_size() -> None:
    """Weak evidence has to be visible in the transcript, not inferred."""
    outcome = MutationOutcome(generated=20, total=20, killed=20, survivors=())
    assert "of 20 changed-line mutants" in check_mutation(outcome, 85.0).detail


def test_impl_node_may_not_touch_test_files() -> None:
    """The circularity is one worker authoring both sides (F5, #44).

    Zylos: when one model writes the implementation and the tests, "a
    misreading of the contract doesn't get an independent second look; it
    gets encoded twice". T4's worker fixed the wrong module and rewrote
    the behaviour-pinning test to match; all seven gates passed. An impl
    node that cannot edit tests cannot do that.
    """
    check = check_node_scope("impl", ["orders.py", "tests/test_orders.py"], may_create=True)
    assert check.passed is False
    assert "tests/test_orders.py" in check.detail


def test_test_node_may_not_touch_source_files() -> None:
    """The other direction matters too: a test node that ships the
    implementation alongside its tests has authored both again."""
    check = check_node_scope("test", ["tests/test_orders.py", "orders.py"], may_create=True)
    assert check.passed is False
    assert "orders.py" in check.detail


def test_node_scope_accepts_a_node_that_stays_on_its_side() -> None:
    assert check_node_scope("impl", ["orders.py", "discounts.py"], may_create=True).passed is True
    assert (
        check_node_scope("test", ["tests/test_orders.py", "test_x.py"], may_create=True).passed
        is True
    )


def test_refactor_node_may_touch_both() -> None:
    """A behaviour-preserving refactor moves code and its tests together;
    splitting it across two nodes would leave the first one red."""
    check = check_node_scope(
        "refactor", ["orders.py", "tests/test_orders.py"], added_files=[], may_create=True
    )
    assert check.passed is True


def test_refactor_node_may_not_create_a_file() -> None:
    """#65: a node picks `refactor` to dodge the impl/test split entirely.

    Editing both sides is the exemption's whole point, but creating a file
    is not editing -- it is the split done under the exempt name.
    """
    check = check_node_scope(
        "refactor", ["orders.py", "new_module.py"], added_files=["new_module.py"], may_create=True
    )
    assert check.passed is False
    assert "new_module.py" in check.detail


def test_a_node_without_write_file_may_not_create_a_file() -> None:
    """T3-4 known-bad: `write_file` is the binding that governs creation.

    `allowed_tools` was validated against the global allowlist and then
    consumed by nothing, so withholding `write_file` changed nothing about
    what the node could do. The refactor ban (#65) covered one kind; this
    covers every kind, and it names the tool so the worker can read why.
    """
    check = check_node_scope("refactor", ["n.py"], ["new.py"], may_create=False)
    assert check.passed is False
    assert check.name == "node-scope"
    assert "write_file not in allowed_tools" in check.detail
    assert "new.py" in check.detail


def test_an_impl_node_without_write_file_may_not_create_a_file() -> None:
    """The ban is not the refactor exemption in disguise: an `impl` node
    creating a file was in scope for its kind and is still refused."""
    check = check_node_scope("impl", ["n.py"], ["new.py"], may_create=False)
    assert check.passed is False
    assert "write_file not in allowed_tools" in check.detail


def test_a_node_without_write_file_that_creates_nothing_still_passes() -> None:
    """T3-4 known-good: the binding governs creation, not editing. A node
    that only changes files it already had loses nothing by omitting
    `write_file`, which is what makes the name a choice rather than a
    formality."""
    check = check_node_scope("impl", ["n.py"], [], may_create=False)
    assert check.passed is True
    assert check.detail == "impl node changed 1 file(s) in scope"


def test_target_files_empty_is_unrestricted() -> None:
    check = check_target_files([], ["n.py", "tests/test_n.py", "README.md"])
    assert check.passed is True
    assert check.name == "target-scope"
    assert check.detail == "unrestricted: no target_files declared"


def test_target_files_within_list_passes() -> None:
    """Known-good (T3-2): every touched file is named, so the node stays
    inside the scope it declared."""
    check = check_target_files(["n.py", "tests/test_n.py"], ["n.py"])
    assert check.passed is True
    assert check.detail == "1 touched file(s) within 2 target(s)"


def test_target_files_outside_list_fails_and_names_the_stray() -> None:
    """Known-bad (T3-2, #64): T4's worker fixed the wrong module; a node
    that had named its module would have been stopped here."""
    check = check_target_files(["orders.py"], ["orders.py", "discounts.py", "new_module.py"])
    assert check.passed is False
    assert check.detail == "touched file(s) outside target_files: discounts.py, new_module.py"


def test_impl_node_takes_the_real_differential_not_the_refactor_branch() -> None:
    """An impl node changes no tests, but it is not behaviour-preserving.

    Its tests were written by the test node it depends on and already
    fail at its baseline. Routing it to _check_behaviour_preserved would
    grade a behaviour change on a refactor's evidence.
    """
    check = check_red_phase(
        (PYTEST_TESTS_FAILED,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orders.py"],
        tests_changed=False,
        kind="impl",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=10, total=10, killed=10, survivors=()),
    )
    assert check.passed is True
    assert "behaviour preserved" not in check.detail


def test_refactor_node_still_takes_the_behaviour_preserving_branch() -> None:
    check = check_red_phase(
        (0,) * RED_PHASE_SAMPLES,
        lambda: 0,
        baseline_output="",
        changed_files=["orders.py"],
        tests_changed=False,
        kind="refactor",
        coverage=GateCheck(name="coverage", passed=True),
        mutation=MutationOutcome(generated=10, total=10, killed=10, survivors=()),
    )
    assert "behaviour preserved" in check.detail


def test_test_node_must_contribute_a_property() -> None:
    """Two happy-path examples over an unbounded domain is the predicted
    output, not an anomaly (F1). T1 shipped a regex accepting
    `.u@example.com` and `user@example..com` with 7/7 gates green,
    because its two tests probed neither.
    """
    examples_only = {
        "tests/test_v.py": (
            "def test_valid():  # REQ-001\n    assert valid('a@b.co')\n\n"
            "def test_invalid():  # REQ-001\n    assert not valid('nope')\n"
        )
    }
    check = check_property_coverage("test", examples_only)
    assert check.passed is False
    assert "property" in check.detail.lower()


T1_PROPERTY: Final = (
    "from hypothesis import given\n"
    "from hypothesis import strategies as st\n\n\n"
    "@given(st.from_regex(r'^[a-z]+@[a-z]+\\.[a-z]{2,}$'))\n"
    "def test_round_trip(address):  # REQ-001\n"
    "    assert valid(address)\n"
)


def test_hypothesis_given_counts_as_a_property() -> None:
    """A `@given` property is a property (presence), and at least one of
    them must reject an input (polarity, T6-5).

    Known-bad is T1's own shape: one property, positive, over a regex of
    valid addresses. A validator that returns True for everything
    satisfies it, and F1's did for `.u@example.com`. Known-good adds a
    property over near-misses that asserts the rejection.
    """
    positive_only = {"tests/test_v.py": T1_PROPERTY}
    check = check_property_coverage("test", positive_only)
    assert check.passed is False
    assert check.detail == (
        "1 module(s) drive a property, none rejects an input: "
        "a positive-only property cannot tell the code from one that accepts everything"
    )
    rejecting = T1_PROPERTY + (
        "\n\n@given(st.from_regex(r'^[a-z]+@@[a-z]+\\.[a-z]{2,}$'))\n"
        "def test_double_at_rejected(address):  # REQ-001\n"
        "    assert not valid(address)\n"
    )
    check = check_property_coverage("test", {"tests/test_v.py": rejecting})
    assert check.passed is True
    assert check.detail == "1 module(s) drive a property, one rejects an input"


@pytest.mark.parametrize(
    "body",
    [
        "    assert valid(address) is False\n",
        "    assert valid(address) == False\n",
        "    with pytest.raises(ValueError):\n        parse(address)\n",
        "    with raises(ValueError):\n        parse(address)\n",
    ],
)
def test_property_polarity_recognises_each_rejecting_form(body: str) -> None:
    """`is False`, `== False` and a `raises` block each count as a rejection."""
    source = (
        "import pytest\n"
        "from pytest import raises\n"
        "from hypothesis import given\n"
        "from hypothesis import strategies as st\n\n\n"
        "@given(st.text())\n"
        "def test_rejects(address):  # REQ-001\n" + body
    )
    assert check_property_coverage("test", {"t.py": source}).passed is True


def test_property_polarity_ignores_rejections_outside_a_property() -> None:
    """An example that asserts `not valid(x)` is a case the author chose;
    the rejecting assertion has to sit under `@given` to count. Nor does
    `is True`, `== 1` or `!=` count as rejecting."""
    example_rejects = T1_PROPERTY + (
        "\n\ndef test_double_at():  # REQ-001\n    assert not valid('a@@b.co')\n"
    )
    assert check_property_coverage("test", {"t.py": example_rejects}).passed is False
    for body in (
        "    assert valid(address) is True\n",
        "    assert valid(address) == 1\n",
        "    assert valid(address) != 'x'\n",
        "    with open('x'):\n        pass\n",
    ):
        source = T1_PROPERTY.replace("    assert valid(address)\n", body)
        assert check_property_coverage("test", {"t.py": source}).passed is False, body


def test_property_oracle_binds_the_impl_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3-3: the property alone must kill a sampled mutant of the impl node.

    Known-good: a kill by the property modules passes with the basis. No
    targets: not required. Known-bad: zero kills names the count and the
    modules; no mutants sampled fails; targets without an oracle means the
    runner skipped a run it owed, which fails rather than passes.
    """
    killed = MutationOutcome(killed=3, total=5, generated=5, survivors=("m4", "m5"))
    check = check_property_coverage("impl", {}, oracle=killed, targets=("test_n.py",))
    assert check.passed is True
    assert check.detail == "property killed 3 of 5 mutant(s)"
    assert check.basis == "oracle: killed 3 of 5 mutant(s) by test_n.py"

    untargeted = check_property_coverage("impl", {}, targets=())
    assert untargeted.passed is True
    assert untargeted.detail == "not required: no property targets this change"
    # A pass that judged nothing says so (F21.64 recommendation 69): the
    # basis was None here, and a reader could not tell this apart from a
    # pass an oracle actually earned.
    assert untargeted.basis == "oracle: not run, no property module names a changed file"

    excluded = check_property_coverage("impl", {}, targets=(), out_of_scope=("test_store.py",))
    assert excluded.passed is True
    assert excluded.detail == (
        "not required: no property module in this node's test scope (test_store.py outside it)"
    )
    assert excluded.basis == "oracle: not run, 0 of 1 property module(s) in scope"

    none = MutationOutcome(killed=0, total=5, generated=5, survivors=("m1", "m2", "m3", "m4", "m5"))
    check = check_property_coverage("impl", {}, oracle=none, targets=("test_n.py", "test_m.py"))
    assert check.passed is False
    assert check.detail == (
        "property killed 0 of 5 mutant(s): no discriminating power (test_n.py, test_m.py)"
    )
    empty = MutationOutcome(killed=0, total=0, generated=0, survivors=())
    check = check_property_coverage("impl", {}, oracle=empty, targets=("test_n.py",))
    assert check.passed is False
    assert check.detail == "no mutants sampled for the property oracle (test_n.py)"
    check = check_property_coverage("impl", {}, oracle=None, targets=("test_n.py",))
    assert check.passed is False
    assert check.detail == "property oracle did not run for test_n.py"


def test_property_coverage_does_not_bind_impl_or_refactor_nodes() -> None:
    """An impl node with no property targeting its change is not required
    (the oracle half is tested above; this is not a passing instance of
    it), and a refactor preserves the tests it moves."""
    assert (
        check_property_coverage("impl", {}).detail
        == "not required: no property targets this change"
    )
    assert (
        check_property_coverage("refactor", {"tests/test_v.py": "def test_a():\n    pass\n"}).passed
        is True
    )


def test_property_detection_handles_unparseable_and_bare_decorators() -> None:
    """The syntax gate runs first, but check order is not a guarantee the
    property check may lean on; and a non-`given` decorator must not be
    mistaken for a property."""
    assert check_property_coverage("test", {"t.py": "def broken( :\n"}).passed is False

    bare = {
        "t.py": (
            "import pytest\n"
            "from hypothesis import given\n\n\n"
            "@pytest.mark.parametrize('x', [1])\n"
            "def test_example(x):  # REQ-001\n"
            "    assert x\n\n\n"
            "@given\n"
            "def test_property(value):  # REQ-001\n"
            "    assert not value is None\n"
        )
    }
    assert check_property_coverage("test", bare).passed is True


def test_parametrize_is_examples_not_a_property() -> None:
    """`@pytest.mark.parametrize` is a table of cases the author chose.

    That is precisely what F1 shows is insufficient -- the cases probed
    are the cases already in mind. Only generated inputs count.
    """
    table_only = {
        "t.py": (
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('address', ['a@b.co', 'c@d.co'])\n"
            "def test_valid(address):  # REQ-001\n"
            "    assert valid(address)\n"
        )
    }
    check = check_property_coverage("test", table_only)
    assert check.passed is False
    assert "examples only" in check.detail


def test_refactor_may_not_rewrite_an_existing_assertion() -> None:
    """T4: the worker fixed the wrong module and rewrote the
    behaviour-pinning test to match -- 3.03 became 3.02 -- and all seven
    gates passed. #57 stops an impl node touching tests at all, but a
    refactor may move code and tests together, and "behaviour-preserving"
    means the assertions survive the move.
    """
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.02\n"}
    check = check_assertion_preservation("refactor", baseline, current)
    assert check.passed is False
    assert "test_total" in check.detail


def test_adding_assertions_to_an_existing_test_is_allowed() -> None:
    """Append-only: strengthening a test is the desired direction."""
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {
        "tests/t.py": (
            "def test_total():  # REQ-001\n    assert total() == 3.03\n    assert total() > 0\n"
        )
    }
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_test_nodes_may_rewrite_assertions() -> None:
    """Repairing stale assertions is a test node's job (T2), and #57
    stops it shipping the implementation alongside."""
    baseline = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/t.py": "def test_total():  # REQ-001\n    assert total() == 3.02\n"}
    assert check_assertion_preservation("test", baseline, current).passed is True


def test_moving_a_test_to_another_module_preserves_its_assertions() -> None:
    """A refactor relocating tests is fine while the asserts travel."""
    baseline = {"tests/old.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    current = {"tests/new.py": "def test_total():  # REQ-001\n    assert total() == 3.03\n"}
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_assertion_preservation_ignores_helpers_and_unparseable_sources() -> None:
    """Only `test*` functions carry the behaviour contract, and a source
    the syntax gate will reject must not crash this one."""
    baseline = {
        "tests/t.py": (
            "def _helper():\n    assert 1 == 1\n\n\n"
            "def test_real():  # REQ-001\n    assert total() == 3.03\n"
        ),
        "tests/broken.py": "def test_x( :\n",
    }
    # The helper's assertion vanishes; only the test function is contracted.
    current = {"tests/t.py": "def test_real():  # REQ-001\n    assert total() == 3.03\n"}
    assert check_assertion_preservation("refactor", baseline, current).passed is True


def test_same_test_name_in_two_modules_keeps_both_contracts() -> None:
    """Keying by name merges rather than overwrites.

    Two modules can each define `test_total` over different subjects. If
    the later one replaced the earlier, dropping the first module's
    assertion would go unnoticed -- the looser reading of a name
    collision, in a gate whose whole job is to be the stricter one.
    """
    baseline = {
        "tests/orders.py": "def test_total():  # REQ-001\n    assert order_total() == 3.03\n",
        "tests/invoices.py": "def test_total():  # REQ-002\n    assert invoice_total() == 3.03\n",
    }
    current = {
        "tests/orders.py": "def test_total():  # REQ-001\n    assert order_total() == 3.02\n",
        "tests/invoices.py": "def test_total():  # REQ-002\n    assert invoice_total() == 3.03\n",
    }
    check = check_assertion_preservation("refactor", baseline, current)
    assert check.passed is False
    assert "test_total" in check.detail


# --- T3-7a: a `test` node is a red specification ---------------------------

SPEC_SOURCE: Final = (
    "from hypothesis import given\n"
    "from hypothesis import strategies as st\n\n"
    "from n import f\n\n\n"
    "def test_f_returns_two():  # REQ-001\n"
    "    assert f() == 2\n\n\n"
    "@given(st.integers())\n"
    "def test_f_is_an_int(_value):  # REQ-001\n"
    "    assert isinstance(f(), int)\n\n\n"
    "@given(st.integers())\n"
    "def test_f_is_never_three(_value):  # REQ-001\n"
    "    assert (f() == 3) is False\n"
)


def _spec_inputs(exit_code: int, output: str) -> Tier1Inputs:
    """Inputs the runner would build for a test node whose only diff is a test file."""
    return replace(
        _passing_inputs(),
        ruff_files=["test_n.py"],
        test_runner=lambda _cmd: exit_code,
        changed={("test_n.py", 1)},
        covered=set(),
        baseline_exits=(),
        current_runner=lambda: exit_code,
        flipped_tests={"test_n.py": SPEC_SOURCE},
        mutation=MutationOutcome(killed=0, total=0, generated=0, survivors=()),
        test_output=output,
        workdir_modules=["n", "test_n"],
    )


def test_tests_spec_node_passes_on_a_red_run() -> None:
    """T3-7a known-good: a test node's suite must fail now."""
    check = check_test_command(
        "pytest test_n.py",
        lambda _cmd: PYTEST_TESTS_FAILED,
        kind="test",
        output="1 failed, 1 passed in 0.02s",
    )
    assert check.passed is True
    assert check.detail == "red specification: 1 failing test(s)"


def test_tests_spec_node_passes_on_a_greenfield_import() -> None:
    """The module under test does not exist yet: the impl node will create it."""
    check = check_test_command(
        "pytest test_m.py",
        lambda _cmd: PYTEST_COLLECTION_ERROR,
        kind="test",
        output="E   ModuleNotFoundError: No module named 'm'",
        workdir_modules={"n"},
    )
    assert check.passed is True
    assert check.detail == "red specification: module 'm' does not exist yet"


@pytest.mark.parametrize(
    ("exit_code", "output", "fragment"),
    [
        (0, "2 passed in 0.01s", "exited 0: tests already pass, nothing specified"),
        (PYTEST_TESTS_FAILED, "", "exited 1 but its output counts no failing test"),
        (
            PYTEST_COLLECTION_ERROR,
            "ModuleNotFoundError: No module named 'n'",
            "names existing module 'n'",
        ),
        (PYTEST_COLLECTION_ERROR, "ERROR test_n.py - SyntaxError", "names no missing module"),
        (5, "no tests ran in 0.01s", "exited 5: tests never ran"),
    ],
)
def test_tests_spec_node_known_bad(exit_code: int, output: str, fragment: str) -> None:
    check = check_test_command(
        "pytest test_n.py",
        lambda _cmd: exit_code,
        kind="test",
        output=output,
        workdir_modules={"n"},
    )
    assert check.passed is False
    assert fragment in check.detail


@pytest.mark.parametrize(
    ("code", "word"), [(SHELL_TIMEOUT, "hangs"), (TOOL_UNAVAILABLE, "unavailable")]
)
def test_tests_spec_node_hang_and_unavailable_still_fail(code: int, word: str) -> None:
    check = check_test_command(
        "pytest test_n.py", lambda _cmd: code, kind="test", output="1 failed in 0.01s"
    )
    assert check.passed is False
    assert word in check.detail


def test_tests_impl_node_still_needs_exit_zero() -> None:
    """The default kind is unchanged: exit 1 fails an impl node whatever the output says."""
    check = check_test_command(
        "pytest test_n.py", lambda _cmd: PYTEST_TESTS_FAILED, output="1 failed in 0.01s"
    )
    assert check.passed is False
    assert check.detail == "'pytest test_n.py' exited 1"


def test_red_phase_spec_node_mirrors_the_tests_verdict() -> None:
    """A test node has no baseline leg: red-phase is the tests verdict, restated."""

    def red_phase(spec: GateCheck | None) -> GateCheck:
        # `baseline_exits=()` proves the branch returns before any baseline
        # observation is read: the runner takes none for a test node.
        return check_red_phase(
            (),
            lambda: PYTEST_TESTS_FAILED,
            baseline_output="",
            changed_files=[],
            tests_changed=True,
            kind="test",
            coverage=GateCheck(name="coverage", passed=True),
            mutation=MutationOutcome(killed=0, total=0, generated=0, survivors=()),
            red_spec=spec,
        )

    red = red_phase(
        GateCheck(name="tests", passed=True, detail="red specification: 1 failing test(s)")
    )
    assert red.passed is True
    assert red.detail == "red by construction: the specification fails now"
    green = red_phase(
        GateCheck(
            name="tests", passed=False, detail="'pytest test_n.py' exited 0: nothing specified"
        )
    )
    assert green.passed is False
    assert (
        green.detail == "specification is not red: 'pytest test_n.py' exited 0: nothing specified"
    )
    missing = red_phase(None)
    assert missing.passed is False
    assert missing.detail == "specification is not red: no tests verdict to mirror"


def test_mutation_failed_tool_is_named_not_undecided() -> None:
    """T3-20: a tool that never ran renders as a tool failure (known-bad),
    while a run that decided nothing keeps its wording (known-good)."""
    failed = MutationOutcome(
        killed=0, total=0, generated=0, survivors=("mutmut run exited 1: boom",)
    )
    check = check_mutation(failed, 85.0)
    assert check.passed is False
    assert check.detail == "mutation tool failed: mutmut run exited 1: boom"
    assert check.basis == "sampled n=0"
    undecided = MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut not on PATH",))
    assert check_mutation(undecided, 85.0).detail == "no mutants decided: mutmut not on PATH"


def test_mutation_red_suite_is_named_not_blamed_on_the_tool() -> None:
    """T6-63. Known-bad: mutmut exited non-zero because the node's own
    suite is red, so the detail names the suite -- the worker can act on
    that and cannot act on "the tool failed". Known-good: a genuine tool
    failure keeps T3-20's wording, and an undecided run keeps its own.
    """
    tool = "mutmut run exited 1: failed to collect stats. runner returned 1"
    red = MutationOutcome(killed=0, total=0, generated=0, survivors=(f"suite is red: {tool}",))
    check = check_mutation(red, 85.0)
    assert check.passed is False
    assert check.basis == "sampled n=0"
    assert check.detail == f"mutation not measured: suite is red: {tool}"
    broken = MutationOutcome(killed=0, total=0, generated=0, survivors=(f"{tool}",))
    assert check_mutation(broken, 85.0).detail == f"mutation tool failed: {tool}"


def test_mutation_detail_reports_the_text_only_mutants_left_out() -> None:
    """T6-33: the verdict says how many mutants were excluded as text-only,
    on a pass and on a fail, and the survivor count precedes the names."""
    passing = MutationOutcome(killed=9, total=10, generated=10, survivors=("s1",), text_only=4)
    check = check_mutation(passing, 85.0)
    assert check.passed is True
    assert check.detail == (
        "killed 9 of 10 changed-line mutants (90.0% >= 85.0%); 4 text-only mutant(s) excluded"
    )
    failing = MutationOutcome(
        killed=1, total=7, generated=7, survivors=tuple(f"s{i}" for i in range(6)), text_only=2
    )
    check = check_mutation(failing, 85.0)
    assert check.detail == (
        "killed 1 of 7 changed-line mutants (14.3% < 85.0%): survived 6: s0, s1, s2, s3, s4, "
        "...; 2 text-only mutant(s) excluded"
    )
    assert (
        "excluded"
        not in check_mutation(
            MutationOutcome(killed=9, total=10, generated=10, survivors=("s1",)), 85.0
        ).detail
    )


def test_mutation_detail_names_the_untested_mutants_only_on_a_fail() -> None:
    """P0-3 gate-level pair (M-B5): a failing outcome with `untested > 0`
    appends the count and the reason; the same outcome with `untested ==
    0` renders today's exact string, byte-identical, so the existing
    detail tests above stay pinned unmodified."""
    failing = MutationOutcome(
        killed=1, total=7, generated=7, survivors=tuple(f"s{i}" for i in range(6)), untested=2
    )
    check = check_mutation(failing, 85.0)
    assert check.detail == (
        "killed 1 of 7 changed-line mutants (14.3% < 85.0%): survived 6: s0, s1, s2, s3, s4, "
        "...; 2 untested (no test runs the mutated function)"
    )
    untested_zero = MutationOutcome(
        killed=1, total=7, generated=7, survivors=tuple(f"s{i}" for i in range(6))
    )
    assert check_mutation(untested_zero, 85.0).detail == (
        "killed 1 of 7 changed-line mutants (14.3% < 85.0%): survived 6: s0, s1, s2, s3, s4, ..."
    )
    # A pass never carries the suffix (the spec scopes it to a fail).
    passing_but_untested = MutationOutcome(
        killed=9, total=10, generated=10, survivors=("s1",), untested=1
    )
    assert "untested" not in check_mutation(passing_but_untested, 85.0).detail


# --- T6-29c: the verdict carries the gap it found ---------------------------


FIXTURES: Final = Path(__file__).parent / "fixtures"


def _rewritten(diff: str, path: str) -> tuple[str, set[int]]:
    """`path` as `diff` leaves it, and the line numbers the diff added.

    The fixtures rewrite each file in one whole-file hunk, so the new side
    is the file; `+` lines are what the node wrote and context lines are
    what it kept.
    """
    lines = diff.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"diff --git a/{path} "))
    after = (i for i in range(start + 1, len(lines)) if lines[i].startswith("diff --git "))
    body = lines[start : next(after, len(lines))]
    hunk = next(i for i, line in enumerate(body) if line.startswith("@@ "))
    out: list[str] = []
    added: set[int] = set()
    for line in body[hunk + 1 :]:
        if line.startswith("+"):
            out.append(line[1:])
            added.add(len(out))
        elif line.startswith(" "):
            out.append(line[1:])
    return "".join(out), added


def _baseline(diff: str, path: str) -> str:
    """`path` as it stood before `diff`, from that same whole-file hunk.

    The fixtures carry full context, so the `-` and context lines are the
    original file. T6-42 compares what was defined before against what is
    defined after, so it needs both sides of one diff.
    """
    lines = diff.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"diff --git a/{path} "))
    after = (i for i in range(start + 1, len(lines)) if lines[i].startswith("diff --git "))
    body = lines[start : next(after, len(lines))]
    hunk = next(i for i, line in enumerate(body) if line.startswith("@@ "))
    return "".join(line[1:] for line in body[hunk + 1 :] if line.startswith(("-", " ")))


def _fixture_tree(name: str) -> tuple[dict[str, str], dict[str, set[int]]]:
    """The three modules a round-3e implementation draw wrote, plus the
    suite that names their public surface."""
    diff = (FIXTURES / name).read_text()
    sources: dict[str, str] = {}
    added: dict[str, set[int]] = {}
    for path in ("money.py", "accounts.py", "fees.py"):
        sources[path], added[path] = _rewritten(diff, path)
    sources["tests/test_fees.py"] = (
        "from fees import FEES, FLAT_FEE, apply_fee, fee_for, total_fees\n"
        "from accounts import Account, transfer\n"
        "from money import quantize, to_decimal, validate_currency\n"
    )
    return sources, added


def test_dead_additions_rejects_the_round3e_repeated_block() -> None:
    """The real artifact: nine of eleven gates passed it (F21.16 §1).

    `fees.py` grew from 41 lines to 1337 by repeating one 21-line block,
    and mutation still read 88.8% over 80 mutants because a `pass` body
    admits no mutant while importing the module executes it. Nothing in
    the tree mentions any of those names, and the suite does not notice
    them going, so they implement nothing.
    """
    sources, added = _fixture_tree("degenerate_round3e.diff")
    seen: dict[str, str] = {}

    def run_without(edited: Mapping[str, str]) -> int:
        seen.update(edited)
        return 0

    check = check_dead_additions(sources, added, suite_passed=True, run_without=run_without)
    assert not check.passed
    assert check.name == "dead-code"
    assert "fees.py adds" in check.detail
    assert "_ensure_executed (60 copies)" in check.detail
    assert "_hypothesis_helper (60 copies)" in check.detail
    assert "implement no requirement" in check.detail
    assert check.basis == "dead-definitions=4"
    # What it handed the runner is the module without them and with its
    # own work intact -- not a reformatted file.
    assert "_noop" not in seen["fees.py"]
    assert "def total_fees(count, currency=" in seen["fees.py"]
    assert seen["fees.py"] in sources["fees.py"] or len(seen["fees.py"]) < len(sources["fees.py"])


def test_dead_additions_accepts_the_same_draw_without_the_repeated_block() -> None:
    """The discriminating half: the identical draw, cut before the block.

    Same three modules, same public functions, same tests naming them --
    so what the gate rejects above is the block and nothing else about the
    draw. The suite is never re-run because no candidate is found.
    """
    sources, added = _fixture_tree("implementation_round3e.diff")

    def run_without(edited: Mapping[str, str]) -> int:
        never = "no candidate, so the suite must not be re-run"
        raise AssertionError(never)

    check = check_dead_additions(sources, added, suite_passed=True, run_without=run_without)
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def _padded_tree(count: int = 50) -> tuple[dict[str, str], dict[str, set[int]]]:
    """Round 3h's shape: many helpers named once each, beside real work.

    The frozen round-3e draw repeats twenty names; this one repeats none.
    """
    real = (
        "def fee_for(amount):\n    return _coerce(amount)\n\n\n"
        "def _coerce(amount):\n    return amount\n"
    )
    pad = "\n\n".join(
        f'def _legacy_usd_round_half_up_{n}dp(a):\n    """Legacy rounding helper."""\n    return a'
        for n in range(1, count + 1)
    )
    source = real + "\n\n" + pad + "\n"
    sources = {"fees.py": source, "tests/test_fees.py": "from fees import fee_for\n"}
    added = {"fees.py": set(range(len(real.splitlines()) + 1, len(source.splitlines()) + 1))}
    return sources, added


def test_dead_additions_rejects_many_distinct_names_used_once() -> None:
    """The other degeneration shape: no name repeats and all are dead.

    Round 3e's frozen draw emits twenty names, three of them sixty times,
    so a check counting repeats would reject it. Round 3h's `n2` attempt 1
    draw 2 emits 192 definitions with 192 distinct names, 187 mentioned
    nowhere but their own `def` line (F21.30), and a repeat count would
    read it as clean. This gate asks whether anything depends on the
    definition, so both shapes land the same way; that is what this pins.
    The basis counts every dead name, so an implementation that
    special-cased repetition fails here rather than merely scoring lower.

    The bytes are built, not restored: 3h draw 2's patch is malformed
    (`corrupt patch at line 182`) and no post-image tree exists.
    """
    sources, added = _padded_tree()
    seen: dict[str, str] = {}

    def run_without(edited: Mapping[str, str]) -> int:
        seen.update(edited)
        return 0

    check = check_dead_additions(sources, added, suite_passed=True, run_without=run_without)
    assert not check.passed
    assert check.name == "dead-code"
    assert check.basis == "dead-definitions=50"
    assert "implement no requirement" in check.detail
    assert "copies)" not in check.detail
    assert "_legacy_usd_round_half_up_1dp" not in seen["fees.py"]
    assert "def fee_for(amount):" in seen["fees.py"]


def test_dead_additions_accepts_many_distinct_names_the_suite_misses() -> None:
    """The discriminating half: the identical fifty names, suite red.

    Same tree, same count, same spelling; only the suite's answer to
    their removal differs. So the rejection above is about nothing
    depending on them, not about how much the node added -- the property
    that keeps this gate from being a line count under another name.
    """
    sources, added = _padded_tree()
    check = check_dead_additions(sources, added, suite_passed=True, run_without=lambda edited: 1)
    assert check.passed
    assert check.basis == "dead-candidates=50"
    assert "the suite fails without them, so they carry the work" in check.detail


def test_dead_additions_keeps_a_private_helper_the_suite_depends_on() -> None:
    """A private helper nothing names is a candidate, not a verdict.

    `_round_half_up` is mentioned by no other module and by no test, which
    is true of every implementation detail. Removing it takes its callers
    with it and the suite goes red, so the gate passes and says why.
    """
    source = (
        "def _round_half_up(value):\n"
        "    return int(value + 0.5)\n"
        "\n"
        "\n"
        "def quantize(value):\n"
        "    return _round_half_up(value)\n"
    )
    sources = {"money.py": source, "tests/test_money.py": "from money import quantize\n"}
    added = {"money.py": set(range(1, 7))}
    check = check_dead_additions(sources, added, suite_passed=True, run_without=lambda edited: 1)
    assert check.passed
    assert check.detail == (
        "money.py adds _round_half_up; the suite fails without them, so they carry the work"
    )
    assert check.basis == "dead-candidates=1"


def test_dead_additions_reads_a_name_spelled_as_a_string() -> None:
    """A name reached by string is mentioned: `__all__`, a marker, a getattr.

    The identifier set is deliberately over-wide because it decides what
    is NOT dead, and a mention this misses fails a node for code something
    uses.
    """
    sources = {
        "m.py": "def _handler():\n    return 1\n",
        "app.py": 'import m\n\nrun = getattr(m, "_handler")\n',
    }
    check = check_dead_additions(
        sources,
        {"m.py": {1, 2}},
        suite_passed=True,
        run_without=lambda edited: 0,
    )
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def test_dead_additions_says_nothing_when_the_suite_is_already_red() -> None:
    """Removing code from a failing tree proves nothing about the code."""
    sources, added = _fixture_tree("degenerate_round3e.diff")

    def run_without(edited: Mapping[str, str]) -> int:
        never = "a red suite must not be re-run"
        raise AssertionError(never)

    check = check_dead_additions(sources, added, suite_passed=False, run_without=run_without)
    assert check.passed
    assert check.basis == "suite=red"
    assert check.detail == "the suite is already failing; removing anything proves nothing"


def test_dead_additions_leaves_an_unparseable_module_to_the_syntax_gate() -> None:
    """Two gates must not report the same defect in different words."""
    check = check_dead_additions(
        {"m.py": "def _f(:\n", "t.py": "x = 1\n"},
        {"m.py": {1}},
        suite_passed=True,
        run_without=lambda edited: 0,
    )
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def test_dead_additions_skips_a_changed_path_with_no_source(tmp_path: Path) -> None:
    """`added` can name a file the source map does not: a deleted module,
    or one the reader skipped. It is not evidence of anything."""
    check = check_dead_additions(
        {"m.py": "def _f():\n    return 1\n"},
        {"gone.py": {1}},
        suite_passed=True,
        run_without=lambda _edited: 0,
    )
    assert check.passed
    assert check.basis == "modules=1"


def test_dead_additions_reads_mentions_past_a_module_that_does_not_parse() -> None:
    """One unparseable file elsewhere must not turn every private name dead.

    The syntax gate fails the node for it; this check skips that file when
    collecting mentions, so the names it can still read keep protecting.
    """
    sources = {
        "m.py": "def _f():\n    return 1\n",
        "broken.py": "def oops(:\n",
        "uses.py": "from m import _f\n",
    }

    def run_without(edited: Mapping[str, str]) -> int:
        never = "_f is mentioned by uses.py, so there is nothing to re-run"
        raise AssertionError(never)

    check = check_dead_additions(
        sources, {"m.py": {1, 2}}, suite_passed=True, run_without=run_without
    )
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def test_dead_additions_ignores_a_public_definition_no_test_names_yet() -> None:
    """A public name is the module's surface, and the node that writes the
    tests naming it may not have run: round 3e's `fee_for` is new, public,
    and mentioned nowhere in the tree it was gated in."""
    sources = {"fees.py": "def fee_for(amount):\n    return 1\n", "t.py": "x = 1\n"}
    check = check_dead_additions(
        sources, {"fees.py": {1, 2}}, suite_passed=True, run_without=lambda edited: 0
    )
    assert check.passed
    assert check.detail == "every private definition added is mentioned elsewhere in the tree"


def test_public_deletions_rejects_the_round3d_repair_that_deleted_the_api() -> None:
    """The real artifact: n2's repair sealed by removing what it could not fix.

    The node was handed a failing `Account.to_dict` and deleted `to_dict`,
    `from_dict`, `__eq__` and `__repr__` rather than repair them. Every
    gate passed, because the tree it left behind calls none of them and
    the suite it ran was the tree's own. Recovered from the run's dangling
    blobs; no attempt snapshot existed to read it from (T6-34).
    """
    diff = (FIXTURES / "repair_deletes_public_round3d.diff").read_text()
    before = _baseline(diff, "accounts.py")
    after, _ = _rewritten(diff, "accounts.py")
    check = check_public_deletions({"accounts.py": before}, {"accounts.py": after})
    assert not check.passed
    assert check.name == "public-deletions"
    assert check.detail == (
        "accounts.py no longer defines Account.__eq__, Account.__repr__, "
        "Account.from_dict, Account.to_dict; "
        "other modules and later nodes still expect them"
    )
    assert check.basis == "deleted-public=4"


@pytest.mark.parametrize("name", ["implementation_round3e.diff", "degenerate_round3e.diff"])
def test_public_deletions_accepts_a_whole_file_rewrite_of_the_same_methods(name: str) -> None:
    """The discriminating half: a rewrite is not a deletion.

    Both round-3e draws rewrite `accounts.py` end to end, so each of those
    four methods appears as a `-` line and again as a `+` line. A check
    reading the diff would reject them; this one reads the tree the node
    left and passes. The degenerate draw is here on purpose: T6-41 is what
    rejects its repeated block, and T6-42 must not double as that gate.
    """
    diff = (FIXTURES / name).read_text()
    paths = ("money.py", "accounts.py", "fees.py")
    before = {path: _baseline(diff, path) for path in paths}
    after = {path: _rewritten(diff, path)[0] for path in paths}
    check = check_public_deletions(before, after)
    assert check.passed
    assert check.detail == "every public definition the baseline had is still defined"
    assert check.basis == "baseline-modules=3"


def test_public_deletions_ignores_a_private_helper_going() -> None:
    """Deleting an implementation detail is refactoring, not amputation."""
    before = {"m.py": "def _helper():\n    return 1\n\n\ndef api():\n    return _helper()\n"}
    after = {"m.py": "def api():\n    return 1\n"}
    check = check_public_deletions(before, after)
    assert check.passed
    assert check.basis == "baseline-modules=1"


def test_public_deletions_ignores_the_methods_of_a_private_class() -> None:
    """A private class's surface is private too, however it is spelled."""
    before = {"m.py": "class _Impl:\n    def run(self):\n        return 1\n"}
    after = {"m.py": "x = 1\n"}
    assert check_public_deletions(before, after).passed


def test_public_deletions_counts_a_dunder_as_public() -> None:
    """`__eq__` reads private by prefix and is the caller-facing contract.

    Three of round 3d's four deletions were dunders, so a rule keyed on
    the leading underscore alone would have admitted the artifact this
    gate exists to reject.
    """
    before = {"m.py": "class A:\n    def __eq__(self, other):\n        return True\n"}
    after = {"m.py": "class A:\n    pass\n"}
    check = check_public_deletions(before, after)
    assert not check.passed
    assert check.detail.startswith("m.py no longer defines A.__eq__;")
    assert check.basis == "deleted-public=1"


def test_public_deletions_names_a_module_that_went_entirely() -> None:
    """A module the node removed defines nothing, so everything it had is gone."""
    before = {"m.py": "def api():\n    return 1\n\n\nclass A:\n    pass\n"}
    check = check_public_deletions(before, {})
    assert not check.passed
    assert check.detail == (
        "m.py no longer defines A, api; other modules and later nodes still expect them"
    )
    assert check.basis == "deleted-public=2"


def test_public_deletions_reports_every_module_it_found() -> None:
    """Two modules stripped are two sentences, ordered by path, one basis."""
    before = {"b.py": "def beta():\n    return 1\n", "a.py": "def alpha():\n    return 1\n"}
    check = check_public_deletions(before, {"a.py": "", "b.py": ""})
    assert check.detail == (
        "a.py no longer defines alpha; b.py no longer defines beta; "
        "other modules and later nodes still expect them"
    )
    assert check.basis == "deleted-public=2"


def test_public_deletions_leaves_a_broken_result_to_the_syntax_gate() -> None:
    """Unparsable sources report nothing here: syntax already fails the node,
    and guessing at definitions in a half-written file would name the wrong
    ones."""
    before = {"m.py": "def api():\n    return 1\n"}
    assert check_public_deletions(before, {"m.py": "def broken(:\n"}).passed
    assert check_public_deletions({"m.py": "def broken(:\n"}, {"m.py": ""}).passed


def test_public_deletions_passes_a_baseline_with_nothing_public_to_lose() -> None:
    """A module of constants has no public definitions, so it cannot lose any."""
    check = check_public_deletions({"conf.py": "TIMEOUT = 5\n"}, {"conf.py": ""})
    assert check.passed
    assert check.basis == "baseline-modules=1"


def test_public_deletions_accepts_an_async_definition_kept() -> None:
    """`async def` is a definition; a check reading only `FunctionDef` would
    think every async function had been deleted."""
    before = {"m.py": "async def fetch():\n    return 1\n"}
    assert check_public_deletions(before, {"m.py": "async def fetch():\n    return 2\n"}).passed
    gone = check_public_deletions(before, {"m.py": "x = 1\n"})
    assert not gone.passed
    assert gone.detail.startswith("m.py no longer defines fetch;")


# The eleven lines round 3g's coverage gate named on `n2.r1` attempt 1 --
# the tree that passes 16 of 16 hidden accounts-and-fees tests (F21.21).
_ROUND3G_UNCOVERED: Final = (
    ("accounts.py", 27),
    ("accounts.py", 97),
    ("accounts.py", 105),
    ("accounts.py", 106),
    ("accounts.py", 107),
    ("accounts.py", 108),
    ("accounts.py", 109),
    ("accounts.py", 114),
    ("accounts.py", 117),
    ("accounts.py", 126),
    ("fees.py", 28),
)
_ROUND3G_COVERED: Final = tuple(("accounts.py", n) for n in range(200, 255))


def test_coverage_defers_round3g_lines_while_a_test_node_is_owed() -> None:
    """Known-good (T6-53): the artifact ground truth accepts, accepted.

    `n2.r1` had to rewrite `Account.to_dict` because REQ-002 replaces the
    scalar balance with a per-currency map, but the record shape it
    writes is REQ-004's -- the task prompt's section 7, `store.py` -- and
    the only file that can exercise it, `tests/test_store.py`, belongs to
    `n3`, which had not run. An impl node may not write tests, so the
    gate was asking a question no node had been able to answer.
    """
    changed = {*_ROUND3G_UNCOVERED, *_ROUND3G_COVERED}
    check = check_changed_line_coverage(changed, set(_ROUND3G_COVERED), 100.0, ("n3",))
    assert check.passed
    assert check.detail.startswith("deferred, no test node has run that can reach accounts.py:27")
    assert check.basis == "changed-lines=66 deferred-lines=11 owed=n3"


def test_coverage_still_fails_the_same_lines_with_no_test_node_owed() -> None:
    """The discriminating half: deferral is a schedule, not an exemption.

    Identical inputs, empty `owed`. If this passed, T6-53 would have
    turned the coverage gate off rather than moved when it asks.
    """
    changed = {*_ROUND3G_UNCOVERED, *_ROUND3G_COVERED}
    check = check_changed_line_coverage(changed, set(_ROUND3G_COVERED), 100.0)
    assert not check.passed
    assert check.detail.startswith("no test runs accounts.py:27")
    assert check.basis == "changed-lines=66"


def test_coverage_deferral_admits_a_node_that_adds_code_nothing_runs() -> None:
    """What T6-53 admits, exhibited rather than described (WORKPLAN 0.6).

    This is the known-bad the rule now lets through: a node adds a
    private helper no test reaches, and while any test node is owed it
    seals. The alternative was failing the run at drain, which fails on
    the arm's own suite while the hidden suite that decides the task is a
    different one -- so it would fail runs whose artifact is correct.
    The admission is bounded by the half above: once nothing is owed, the
    same line fails.
    """
    changed = {("m.py", 1), ("m.py", 2)}
    check = check_changed_line_coverage(changed, {("m.py", 1)}, 100.0, ("n3",))
    assert check.passed
    assert check.basis == "changed-lines=2 deferred-lines=1 owed=n3"


def test_coverage_is_unchanged_when_every_line_runs_and_a_node_is_owed() -> None:
    """Vacuity: `owed` may not turn a pass into a deferral."""
    changed = {("m.py", 1)}
    check = check_changed_line_coverage(changed, changed, 100.0, ("n3", "n4"))
    assert check.passed
    assert check.detail == "every changed line runs"
    assert check.basis == "changed-lines=1"


def _g1_sources() -> tuple[dict[str, str], dict[str, str]]:
    """The real pair from `g1-cw100k/t5-s1`: t5's baseline and what attempt 3 wrote."""
    fixtures = Path(__file__).parent / "fixtures"
    baseline = {"accounts.py": (fixtures / "edit_target_accounts.txt").read_text()}
    after = {"accounts.py": (fixtures / "compelled_accounts_g1.txt").read_text()}
    return baseline, after


def test_coverage_passes_the_draw_it_failed_when_only_compelled_lines_are_uncovered() -> None:
    """Known-good: the artifact three attempts could not get past (T6-75).

    Attempt 3 wrote correct multi-currency code -- its own gate command
    was green at 29 passed and mutation read 92.3% -- and coverage failed
    it on exactly `__eq__`, `__repr__`, `from_dict` and `to_dict`, which
    `public-deletions` had refused to let attempt 2 remove. Nothing calls
    those four: not the rest of the baseline, not the four visible test
    files, not the five hidden ones.
    """
    baseline, after = _g1_sources()
    compelled = compelled_lines(baseline, after)
    uncovered = {("accounts.py", line) for _, line in compelled}
    changed = uncovered | {("accounts.py", 1)}
    covered = {("accounts.py", 1)}

    before = check_changed_line_coverage(changed, covered, 100.0)
    after_check = check_changed_line_coverage(changed, covered, 100.0, (), compelled)

    assert not before.passed, "the old behaviour is what this replaces"
    assert "accounts.py:" in before.detail, "and it failed on these very lines"
    assert after_check.passed
    # The spared lines are recorded, or a node seals with 68 lines set
    # aside and nothing says so.
    assert after_check.basis == f"changed-lines={len(changed)} compelled-lines={len(uncovered)}"

    # With nothing else changed, the whole judgement is compelled and the
    # detail says which case this is rather than claiming tests ran.
    only = check_changed_line_coverage(uncovered, set(), 100.0, (), compelled)
    assert only.passed
    assert "definition the baseline already had" in only.detail


def test_coverage_still_fails_an_uncovered_definition_the_node_invented() -> None:
    """The load-bearing half: the exemption covers the baseline's API, not the node's.

    Same file, same uncovered-lines shape -- but the baseline does not
    define `Account.summary`, so nothing compelled it and nothing excuses
    it. A node that could evade coverage by adding a public method would
    have been handed the `max_mutants=1` exploit in a new costume.
    """
    baseline, after = _g1_sources()
    invented = after["accounts.py"].replace(
        "    def to_dict(self):",
        "    def summary(self):\n        return len(self._balances)\n\n    def to_dict(self):",
        1,
    )
    compelled = compelled_lines(baseline, {"accounts.py": invented})
    body = invented.split("\n")
    summary_line = body.index("    def summary(self):") + 1

    assert ("accounts.py", summary_line) not in compelled
    check = check_changed_line_coverage(
        {("accounts.py", summary_line)}, set(), 100.0, (), compelled
    )
    assert not check.passed
    assert f"accounts.py:{summary_line}" in check.detail


def test_coverage_still_fails_an_uncovered_line_outside_every_compelled_definition() -> None:
    """Module-level code the baseline never had is judged as it always was."""
    baseline, after = _g1_sources()
    compelled = compelled_lines(baseline, after)
    check = check_changed_line_coverage({("accounts.py", 2)}, set(), 100.0, (), compelled)
    assert not check.passed
    assert "no test runs accounts.py:2" in check.detail
