"""`--tier2 shortlist`: the tier-2 mutation verdict as a per-line survivor shortlist.

Contract (scope narrowed from the kill-rate bar): the mutation finding
passes iff no surviving -- or untested -- mutant on a changed line lacks an
accepted reason. The score is still computed and recorded in `basis` (the
finding's cites) but decides nothing. The detail names up to N survivors,
one per line first, each with file:line, the changed line's text and the
mutation. A survivor whose change sits only in a message argument (by AST)
is excluded and counted; one in a statically `text` or `equivalent` class
(`mutant_text.classify`) is set aside and named with the rule.

Under `--tier2 shortlist` an open survivor, and an uncovered changed line,
is `not-proven`: named in the report, not a refusal. `--tier2 score` (the
default) is unchanged.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from halter.auditor import Auditor, AuditorConfig, Findings, behaviour_at
from halter.cli import main
from halter.evidence import (
    MutationOutcome,
    SurvivorDetail,
    message_only_mutant,
    mutation_sample,
    mutation_text,
    run_capture,
)
from halter.gates import (
    check_mutation,
    check_mutation_shortlist,
    set_aside_kind,
    shortlist_order,
)


def _outcome(killed: int, details: list[SurvivorDetail]) -> MutationOutcome:
    return MutationOutcome(
        killed=killed,
        total=killed + len(details),
        generated=killed + len(details),
        survivors=tuple(d[0] for d in details),
        survivor_details=tuple(details),
        untested=sum(1 for d in details if d[1] == "no tests"),
    )


def _d(
    name: str, line: int, status: str = "survived", path: str = "m.py", message: bool = False
) -> SurvivorDetail:
    return (name, status, path, line, f"-    return {line}\n+    return {line + 1}", message)


SOURCE = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(2, 12))


# -- the verdict ---------------------------------------------------------------


def test_a_survivor_fails_even_when_the_score_clears_the_bar() -> None:
    """Mutant 'the score still decides' dies here: 19 of 20 is 95% >= 85%."""
    outcome = _outcome(19, [_d("m1", 3)])
    assert check_mutation(outcome, 85.0).passed  # the old gate passed it
    check = check_mutation_shortlist(outcome, 85.0)
    assert not check.passed
    assert "m.py:3" in check.detail


def test_every_survivor_accepted_passes_below_the_bar() -> None:
    """Known-good: a low score whose survivors all carry a reason."""
    outcome = _outcome(1, [_d("m1", 3), _d("m2", 4)])
    assert not check_mutation(outcome, 85.0).passed
    check = check_mutation_shortlist(outcome, 85.0, accepted={("m.py", 3), ("m.py", 4)})
    assert check.passed
    assert "2 survivor(s) on lines with an accepted reason" in check.detail


def test_an_accepted_line_does_not_excuse_another_line() -> None:
    outcome = _outcome(5, [_d("m1", 3), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0, accepted={("m.py", 3)})
    assert not check.passed
    assert "m.py:4" in check.detail
    assert "m.py:3" not in check.detail


def test_no_survivor_passes_and_the_score_is_recorded_not_decisive() -> None:
    outcome = _outcome(7, [])
    check = check_mutation_shortlist(outcome, 85.0)
    assert check.passed
    assert check.basis == "sampled n=7; score 100.0% vs 85.0% (recorded, not decisive)"
    low = check_mutation_shortlist(_outcome(3, [_d("m1", 2)]), 85.0)
    assert low.basis is not None
    assert "score 75.0%" in low.basis


def test_an_empty_population_keeps_the_old_no_evidence_verdict() -> None:
    for outcome in (
        MutationOutcome(killed=0, total=0, generated=0, survivors=()),
        MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut run exited 1: x",)),
    ):
        check = check_mutation_shortlist(outcome, 85.0)
        assert check == check_mutation(outcome, 85.0)
        assert not check.passed


# -- the shortlist ---------------------------------------------------------------


def test_every_survivor_is_named_when_n_allows_it() -> None:
    """Mutant 'a survivor omitted from the list when N allows it' dies here."""
    outcome = _outcome(10, [_d("m1", 3), _d("m2", 5), _d("m3", 7)])
    check = check_mutation_shortlist(outcome, 85.0, sources={"m.py": SOURCE})
    rows = [r for r in check.detail.splitlines() if r.startswith("- ")]
    assert [r.split()[1] for r in rows] == ["m.py:3", "m.py:5", "m.py:7"]
    assert "more)" not in check.detail
    assert "3 surviving mutant(s)" in check.detail


def test_untested_mutants_are_listed_and_counted() -> None:
    """Mutant 'untested mutants not listed' dies here."""
    outcome = _outcome(4, [_d("m1", 3, "no tests"), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0)
    assert "- m.py:3" in check.detail
    assert "(no tests)" in check.detail
    assert "1 untested" in check.detail
    assert not check_mutation_shortlist(_outcome(4, [_d("m1", 3, "no tests")]), 85.0).passed


def test_n_caps_the_list_and_counts_the_rest() -> None:
    outcome = _outcome(0, [_d(f"m{i}", i) for i in range(2, 9)])
    check = check_mutation_shortlist(outcome, 85.0, shortlist=5)
    rows = [r for r in check.detail.splitlines() if r.startswith("- ")]
    assert len(rows) == 5
    assert check.detail.endswith("(and 2 more)")
    two = check_mutation_shortlist(outcome, 85.0, shortlist=2)
    assert len([r for r in two.detail.splitlines() if r.startswith("- ")]) == 2


def test_a_row_carries_the_line_text_and_the_mutation() -> None:
    outcome = _outcome(0, [_d("m1", 3)])
    check = check_mutation_shortlist(outcome, 85.0, sources={"m.py": SOURCE})
    assert "- m.py:3 `x3 = 3`: mutant m1 (survived): -    return 3 -> +    return 4" in check.detail
    bare = check_mutation_shortlist(outcome, 85.0)
    assert "- m.py:3: mutant m1" in bare.detail


def test_one_mutant_per_line_first_then_the_rest() -> None:
    details = [_d("a", 3), _d("b", 3), _d("c", 4), _d("d", 2, path="a.py")]
    assert [d[0] for d in shortlist_order(details)] == ["d", "a", "c", "b"]


# -- the evidence: every survivor carries its line and its mutation -----------


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: str, show: str) -> None:
    stub = tmp_path / "stub"
    stub.mkdir()
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


SHOW = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"


def test_mutation_sample_records_each_survivor_with_its_line_and_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  m1: survived\n  m2: killed\n  m3: no tests", SHOW)
    work = tmp_path / "w"
    work.mkdir()
    (work / "n.py").write_text("def f():\n    return 2\n")
    outcome = mutation_sample(work, {(str(work / "n.py"), 2)}, 10, test_files=())
    assert outcome.survivor_details == (
        ("m1", "survived", str(work / "n.py"), 2, "-    return 2\n+    return 3", False),
        ("m3", "no tests", str(work / "n.py"), 2, "-    return 2\n+    return 3", False),
    )


def test_mutation_text_keeps_only_hunk_lines() -> None:
    assert mutation_text(SHOW) == "-    return 2\n+    return 3"
    assert mutation_text("# m1: survived\n") == ""


# -- the auditor: tier 2 decides on the shortlist ------------------------------


def _git(root: Path, *argv: str) -> None:
    assert run_capture(["git", *argv], root).exit_code == 0


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "n.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    (root / "n.py").write_text('def f():\n    """Return the answer."""\n    return 2\n')
    (root / "test_n.py").write_text("from n import f\n\n\ndef test_f():\n    assert f() == 2\n")
    return root


SHORTLIST = AuditorConfig(tier2="shortlist")
SHOW3 = "--- n.py\n+++ n.py\n@@ -3 +3 @@\n-    return 2\n+    return 3\n"


def test_tier2_surfaces_one_survivor_at_a_95_percent_score_as_not_proven(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    found = Auditor(tree, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "not-proven", mutation.detail
    assert "- n.py:3 `return 2`: mutant s1 (survived)" in mutation.detail
    assert mutation.cites == (
        "halter.gates.check_mutation_shortlist",
        "sampled n=20; score 95.0% vs 85.0% (recorded, not decisive)",
    )
    assert [(s.path, s.line, s.name, s.source) for s in found.survivors] == [
        ("n.py", 3, "s1", "return 2")
    ]
    assert found.survivors[0].behaviour == "`f`: Return the answer."
    assert found.survivors[0].mutation == "-    return 2\n+    return 3"
    assert Findings.from_dict(found.to_dict()).survivors == found.survivors
    assert found.passed  # surfaced, not refused


def test_tier2_passes_with_every_mutant_killed(tree: Path) -> None:
    found = Auditor(tree, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "pass", mutation.detail
    assert found.survivors == ()
    assert "survivors" not in found.to_dict()


def test_score_mode_is_the_default_and_keeps_the_old_verdict(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag off: one survivor at 95% passes on the bar, cited as before."""
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    assert AuditorConfig().tier2 == "score"
    found = Auditor(tree).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "pass"
    assert mutation.cites == ("halter.gates.check_mutation", "sampled n=20")
    assert mutation.detail == "killed 19 of 20 changed-line mutants (95.0% >= 85.0%)"
    assert found.survivors == ()
    assert "survivors" not in found.to_dict()
    assert Auditor(tree)._key(2, "t") != Auditor(tree, config=SHORTLIST)._key(2, "t")


def test_the_shortlist_size_is_configurable_and_keys_the_cache(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join(f"  s{i}: survived" for i in range(3))
    _stub(tmp_path, monkeypatch, results, SHOW3)
    one = Auditor(tree, config=AuditorConfig(tier2="shortlist", mutant_shortlist=1))
    mutation = next(f for f in one.tier2().findings if f.gate == "mutation")
    assert len([r for r in mutation.detail.splitlines() if r.startswith("- ")]) == 1
    assert "(and 2 more)" in mutation.detail
    assert len(one.tier2().survivors) == 3
    assert one._key(2, "t") != Auditor(tree, config=SHORTLIST)._key(2, "t")


def test_behaviour_at_names_the_innermost_def() -> None:
    src = (
        'def outer():\n    """Outer doc.\n\n    More."""\n'
        "    def inner():\n        return 1\n    return inner\nX = 1\n"
    )
    assert behaviour_at(src, 6) == "`inner`"
    assert behaviour_at(src, 7) == "`outer`: Outer doc."
    assert behaviour_at(src, 8) == "module top level"
    assert behaviour_at("def (:\n", 1) == "module top level"


def test_tier2_shortlist_implies_tiered_and_surfaces_the_survivor(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "\n".join([*(f"  k{i}: killed" for i in range(19)), "  s1: survived"])
    _stub(tmp_path, monkeypatch, results, SHOW3)
    out, err = io.StringIO(), io.StringIO()
    argv = ["--repo", str(tree), "--no-cache", "--json"]
    code = main([*argv, "--tier2", "shortlist", "--mutant-shortlist", "1"], stdout=out, stderr=err)
    payload = json.loads(out.getvalue())
    assert code == 0, err.getvalue()  # the survivor is surfaced, not refused
    mutation = next(f for t in payload["tiers"] for f in t["findings"] if f["gate"] == "mutation")
    assert mutation["verdict"] == "not-proven"
    assert "- n.py:3 `return 2`: mutant s1 (survived)" in mutation["detail"]
    assert payload["tiers"][-1]["survivors"][0]["name"] == "s1"
    assert "survivor_details" not in out.getvalue()
    text = io.StringIO()
    assert main([*argv[:-1], "--tier2", "shortlist"], stdout=text, stderr=err) == 0
    assert "not-proven     mutation" in text.getvalue()
    assert text.getvalue().endswith("verdict: accept\n")
    out = io.StringIO()
    assert main([*argv, "--tiered"], stdout=out, stderr=err) == 0  # score mode: 95% passes


def test_the_default_report_leaves_survivor_details_out(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    out = io.StringIO()
    main(["--repo", str(tree), "--no-cache", "--json"], stdout=out, stderr=io.StringIO())
    assert json.loads(out.getvalue())["mutation"]["total"] == 2
    assert "survivor_details" not in out.getvalue()


# -- message-only survivors are excluded by AST ---------------------------------


def _show(old: str, new: str) -> str:
    return f"--- m.py\n+++ m.py\n@@ -1 +1 @@\n-{old}\n+{new}\n"


GUARD = (
    "def g(x):\n    if x < 0:\n"
    '        raise ValueError(f"bad {x}")\n    log.info("x=%s", x)\n    return x\n'
)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ('        raise ValueError(f"bad {x}")', "        raise ValueError(None)", True),
        ('    log.info("x=%s", x)', '    log.info("XXx=%sXX", x)', True),
        ('    log.info("x=%s", x)', "    log.info(None, x)", True),
        # known-bad: the comparison that decides whether to raise
        ("    if x < 0:", "    if x <= 0:", False),
        # known-bad: the call target, not its argument
        ('    log.info("x=%s", x)', '    log.debug("x=%s", x)', False),
        ("    return x", "    return None", False),
        # cannot be placed in the source: never message-only
        ("    return y", "    return None", False),
    ],
)
def test_message_only_is_decided_on_the_ast(old: str, new: str, expected: bool) -> None:
    assert message_only_mutant(_show(old, new), GUARD, "m1") is expected


def test_a_string_outside_a_message_call_is_not_message_only() -> None:
    def verdict(line: str, new: str) -> bool:
        return message_only_mutant(
            _show(f"    {line}", f"    {new}"), f"def g():\n    {line}\n", "m1"
        )

    assert not verdict('return "abc"', 'return "XXabcXX"')
    assert verdict('return MyError("abc")', "return MyError(None)")
    assert verdict('raise make("abc")', "raise make(None)")
    assert not verdict('return fmt.info("abc")', "return fmt.info(None)")
    assert not verdict('return get()("abc")', "return get()(None)")
    assert verdict('log.info("abc")', 'log.info("abc", x)')
    assert not verdict("global a", "global b")
    assert not verdict('log.info("abc")', "")


def test_an_unparseable_or_empty_hunk_is_not_message_only() -> None:
    assert not message_only_mutant("--- m.py\n+++ m.py\n", GUARD, "m1")
    assert not message_only_mutant(_show("    return x", "    return x +"), GUARD, "m1")
    assert not message_only_mutant(_show("    return x", "    return x"), GUARD, "m1")
    assert not message_only_mutant(_show("x", "y"), "def (:\n", "m.x_g__mutmut_1")
    assert not message_only_mutant(_show("x", "y"), GUARD, "m.x_nope__mutmut_1")


def test_message_only_survivors_leave_the_shortlist_counted() -> None:
    outcome = _outcome(9, [_d("m1", 3, message=True), _d("m2", 4)])
    check = check_mutation_shortlist(outcome, 85.0)
    assert not check.passed
    assert "1 message-only survivor(s) excluded" in check.detail
    assert "m.py:3" not in check.detail
    only = check_mutation_shortlist(_outcome(9, [_d("m1", 3, message=True)]), 85.0)
    assert only.passed


# -- statically equivalent / text survivors are set aside -----------------------

STOCK = """from decimal import ROUND_HALF_UP, Decimal


def _check_int(value, name):
    if not isinstance(value, int):
        raise TypeError(name)


class Inventory:
    def __init__(self):
        self.items = {}

    def scale(self, percent):
        _check_int(percent, "percent")
        factor = Decimal(100 + percent) / 100
        return (Decimal(5) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP)

    def pop(self, sku):
        if sku not in self.items:
            raise KeyError(sku)
        return self.items.pop(sku)
"""


def _mshow(name: str, old: str, new: str) -> str:
    """A `mutmut show` of a method mutant: the def at column 0, as mutmut prints it."""
    return f"# {name}: survived\n--- stock.py\n+++ stock.py\n@@ -1,3 +1,3 @@\n-{old}\n+{new}\n"


# (mutant, removed line, added line, message-only, set-aside kind); the lines are
# dedented by the def's own indent, as mutmut shows a method.
STOCK_MUTANTS = [
    ("pop__mutmut_1", "        raise KeyError(sku)", "        raise KeyError(None)", True, "text"),
    (
        "scale__mutmut_1",
        '    _check_int(percent, "percent")',
        "    _check_int(percent, None)",
        False,
        "text",
    ),
    (
        "scale__mutmut_2",
        "    return (Decimal(5) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP)",
        "    return (Decimal(5) * factor).quantize(Decimal(2), rounding=ROUND_HALF_UP)",
        False,
        "equivalent",
    ),
    ("pop__mutmut_2", "    if sku not in self.items:", "    if sku in self.items:", False, None),
]


def _stock_details(names: set[str]) -> list[SurvivorDetail]:
    found = []
    for i, (short, old, new, _, _) in enumerate(STOCK_MUTANTS):
        if short not in names:
            continue
        name = f"stock.xǁInventoryǁ{short}"
        shown = _mshow(name, old, new)
        found.append(
            (
                name,
                "survived",
                "stock.py",
                i + 1,
                mutation_text(shown),
                message_only_mutant(shown, STOCK, name),
            )
        )
    return found


def test_each_stock_mutant_lands_in_its_class() -> None:
    """Known-good and known-bad instances of message-only and each set-aside kind."""
    for short, _, _, message, kind in STOCK_MUTANTS:
        (detail,) = _stock_details({short})
        assert detail[5] is message, short
        if not message:
            assert set_aside_kind(detail) == kind, short


def test_a_tree_is_admitted_once_equivalent_and_text_survivors_are_set_aside() -> None:
    """Known-good: the only survivors are a message swap (excluded), a
    field-name swap (text) and a quantize exponent swap (equivalent)."""
    details = _stock_details({"pop__mutmut_1", "scale__mutmut_1", "scale__mutmut_2"})
    got = check_mutation_shortlist(_outcome(20, details), 85.0)
    assert got.passed, got.detail
    assert "1 message-only survivor(s) excluded" in got.detail
    assert "; 2 set aside by a static rule" in got.detail
    assert (
        "mutant stock.xǁInventoryǁscale__mutmut_2 (equivalent: no behaviour can change"
        in got.detail
    )
    assert "(text: only message or argument text changes)" in got.detail


def test_a_behaviour_survivor_beside_set_aside_ones_stays_open() -> None:
    """Known-bad: a comparison-operator survivor is behaviour, never set aside;
    an untested one is never set aside either."""
    boundary = ("m.x_f__mutmut_1", "survived", "m.py", 3, "-    if p < 0:\n+    if p <= 0:", False)
    untested = (
        "m.x_f__mutmut_2",
        "no tests",
        "m.py",
        4,
        "-    x = Decimal(1)\n+    x = Decimal(2)",
        False,
    )
    assert set_aside_kind(boundary) is None
    assert set_aside_kind(untested) is None
    got = check_mutation_shortlist(
        _outcome(
            10,
            [
                *_stock_details({"pop__mutmut_1", "scale__mutmut_1", "scale__mutmut_2"}),
                boundary,
                untested,
            ],
        ),
        85.0,
    )
    assert not got.passed
    assert got.detail.startswith("2 surviving mutant(s)")
    assert "mutant m.x_f__mutmut_1 (survived)" in got.detail


# -- coverage is a locator (not-proven) under --tier2 shortlist ---------------


@pytest.fixture
def uncovered(tree: Path) -> Path:
    """`tree` plus an untested changed module: coverage fails in score mode."""
    (tree / "u.py").write_text("def g():\n    return 7\n")
    return tree


def _gate(found: Findings, gate: str) -> str:
    return next(f.verdict for f in found.findings if f.gate == gate)


def test_score_mode_still_fails_coverage_and_blocks_tier2(uncovered: Path) -> None:
    auditor = Auditor(uncovered)
    assert _gate(auditor.tier1(), "coverage") == "fail"
    assert _gate(auditor.tier2(), "mutation") == "blocked"


def test_shortlist_coverage_is_not_proven_and_tier2_admits_when_nothing_survives(
    uncovered: Path,
) -> None:
    """Known-good: coverage fails, every mutant is killed; the audit passes."""
    auditor = Auditor(uncovered, config=SHORTLIST)
    first = auditor.tier1()
    coverage = next(f for f in first.findings if f.gate == "coverage")
    assert coverage.verdict == "not-proven"
    assert "u.py:2" in coverage.detail
    assert first.passed
    second = auditor.tier2()
    assert _gate(second, "mutation") == "pass"
    assert second.passed


def test_shortlist_with_uncovered_lines_still_names_a_behaviour_survivor(
    uncovered: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known-bad: the survivor is still named (surfaced, not refused)."""
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    found = Auditor(uncovered, config=SHORTLIST).tier2()
    mutation = next(f for f in found.findings if f.gate == "mutation")
    assert mutation.verdict == "not-proven"
    assert "mutant s1 (survived)" in mutation.detail
