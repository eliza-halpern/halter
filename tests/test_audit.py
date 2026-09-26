"""Tests for halter.audit: the check battery over a bare diff, and its verdict cache.

Every fixture's changed source line is exactly `    return 2`: the conftest's
autouse `mutmut` stub reports five killed mutants located on that line, so a
tree with healthy tests passes the mutation check for the stated reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

from halter import audit
from halter.audit import (
    AUDIT_TEST_COMMAND,
    NOT_APPLICABLE,
    AuditError,
    AuditResult,
    audit_node,
    audit_tree,
    gate_surface,
)
from halter.dag import Node
from halter.evidence import run_capture
from halter.journal import SpanRecord, SpanRecorder

BASE_CODE = "def f():\n    return 1\n"
FIXED_CODE = "def f():\n    return 2\n"
TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"

# run_tier1's order at the base; the audit reports it unchanged.
CHECK_ORDER = (
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
)


def read_spans(path: Path) -> list[SpanRecord]:
    """The tool spans the recorder appended to `path`, in order; none if it never wrote."""
    if not path.exists():
        return []
    return [SpanRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _git(root: Path, *argv: str) -> None:
    assert run_capture(["git", *argv], root).exit_code == 0


def _init(root: Path, files: dict[str, str]) -> None:
    """Commit `files` as the baseline of a fresh repository at `root`."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline")


def _hashes(root: Path) -> dict[str, str]:
    """Content hash of every file under `root`, `.git` included."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def untracked_module_tree(tmp_path: Path) -> Path:
    """Pair 1: a tracked change that is fully tested, plus an untracked `m.py` no test imports."""
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "test_n.py": TEST_BODY.format(value=1)})
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    (tree / "m.py").write_text("def g():\n    return 7\n")
    return tree


@pytest.fixture
def clean_tree(tmp_path: Path) -> Path:
    """Pair 2: the change and its test are both new to the index, the test untracked."""
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    return tree


def test_untracked_new_module_is_judged(untracked_module_tree: Path) -> None:
    result = audit_tree(untracked_module_tree)
    assert result.verdict == "refuse"
    coverage = next(check for check in result.checks if check.name == "coverage")
    assert coverage.status == "fail"
    assert "m.py" in coverage.detail


def test_clean_diff_is_accepted_and_plan_checks_are_not_applicable(clean_tree: Path) -> None:
    result = audit_tree(clean_tree)
    assert result.verdict == "accept", [
        (c.name, c.detail) for c in result.checks if c.status == "fail"
    ]
    assert tuple(check.name for check in result.checks) == CHECK_ORDER
    by_name = {check.name: check for check in result.checks}
    assert set(NOT_APPLICABLE) == {
        "node-scope",
        "target-scope",
        "requirement-binding",
        "property-coverage",
    }
    for name, reason in NOT_APPLICABLE.items():
        assert by_name[name].status == "not-applicable"
        assert by_name[name].detail == reason
    assert [c.name for c in result.checks if c.status == "pass"] == [
        name for name in CHECK_ORDER if name not in NOT_APPLICABLE
    ]
    # The refactor rule that a new file is out of scope would fail this diff;
    # node-scope is not-applicable, so its detail must not carry that verdict.
    assert "added file" not in by_name["node-scope"].detail


def test_audited_tree_is_never_written_to(untracked_module_tree: Path) -> None:
    before = _hashes(untracked_module_tree)
    assert ".git/index" in before
    audit_tree(untracked_module_tree)
    assert _hashes(untracked_module_tree) == before
    assert not (untracked_module_tree / ".coverage.tier1").exists()


@pytest.mark.parametrize("noise", [False, True])
def test_noise_is_not_a_change(tmp_path: Path, noise: bool) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "test_n.py": TEST_BODY.format(value=1)})
    if noise:
        (tree / "__pycache__").mkdir()
        (tree / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (tree / ".halter").mkdir()
        (tree / ".halter" / "proofs.jsonl").write_text("{}\n")
        (tree / ".coverage.tier1").write_bytes(b"\x00")
    result = audit_tree(tree)
    assert result.verdict == "nothing-to-audit"
    assert result.checks == ()
    assert result.mutation is None


def test_result_is_portable(clean_tree: Path) -> None:
    result = audit_tree(clean_tree)
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["verdict"] == "accept"
    assert re.fullmatch(r"[0-9a-f]{40}", payload["baseline"])
    assert payload["baseline"] != "HEAD"
    assert payload["mutation"]["total"] == 5
    assert payload["test_command"] == AUDIT_TEST_COMMAND
    assert [c["name"] for c in payload["checks"]] == list(CHECK_ORDER)


def test_nothing_to_audit_result_serialises(tmp_path: Path) -> None:
    _init(tmp_path / "tree", {"n.py": BASE_CODE})
    payload = json.loads(json.dumps(audit_tree(tmp_path / "tree").to_dict()))
    assert payload["checks"] == []
    assert payload["mutation"] is None


def test_from_dict_is_the_exact_inverse_of_to_dict(clean_tree: Path) -> None:
    """`from_dict(to_dict(r))` round-trips every field, including `mutation`'s
    tuple-typed `survivors` and `survivor_lines`, through a real JSON hop."""
    result = audit_tree(clean_tree)
    assert result.mutation is not None
    round_tripped = json.loads(json.dumps(result.to_dict()))
    assert AuditResult.from_dict(round_tripped) == result


def test_from_dict_round_trips_a_nothing_to_audit_result(tmp_path: Path) -> None:
    """The `mutation is None` half of the inverse: nothing-to-audit has no mutation."""
    _init(tmp_path / "tree", {"n.py": BASE_CODE})
    result = audit_tree(tmp_path / "tree")
    assert result.mutation is None
    round_tripped = json.loads(json.dumps(result.to_dict()))
    assert AuditResult.from_dict(round_tripped) == result


def test_baseline_names_an_earlier_commit(tmp_path: Path) -> None:
    """The baseline argument is resolved, so a branch or sha compares against that commit."""
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE})
    (tree / "n.py").write_text(FIXED_CODE)
    _git(tree, "commit", "-am", "second")
    head = audit_tree(tree, "HEAD")
    earlier = audit_tree(tree, "HEAD~1")
    assert head.verdict == "nothing-to-audit"
    assert earlier.verdict != "nothing-to-audit"
    assert earlier.baseline != head.baseline


def test_unknown_baseline_is_an_audit_error(tmp_path: Path) -> None:
    _init(tmp_path / "tree", {"n.py": BASE_CODE})
    with pytest.raises(AuditError, match="no-such-ref"):
        audit_tree(tmp_path / "tree", "no-such-ref")


def test_a_tree_that_is_not_a_git_repository_is_an_audit_error(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(AuditError, match="not a git repository"):
        audit_tree(tmp_path / "plain")


def test_a_linked_worktree_is_an_audit_error(tmp_path: Path) -> None:
    """A `.git` file points at a gitdir outside the copy; `git add -A` there would write to it."""
    _init(tmp_path / "main", {"n.py": BASE_CODE})
    linked = tmp_path / "linked"
    _git(tmp_path / "main", "worktree", "add", "--detach", str(linked))
    assert (linked / ".git").is_file()
    index_before = _hashes(tmp_path / "main")
    with pytest.raises(AuditError, match="not a git repository"):
        audit_tree(linked)
    assert _hashes(tmp_path / "main") == index_before


def test_audit_node_is_a_valid_refactor_node() -> None:
    node = audit_node("pytest -q tests")
    assert Node.model_validate(node.model_dump()) == node
    assert node.kind == "refactor"
    assert node.id == "audit"
    assert node.dependencies == []
    assert node.deterministic_gate.test_command == "pytest -q tests"
    assert "write_file" in node.execution_constraints.allowed_tools
    assert node.execution_constraints.max_context_tokens == 8000
    assert node.requirement_ids == ["REQ-000"]
    assert node.deterministic_gate.mutation_sample.max_mutants == 100
    assert node.deterministic_gate.mutation_sample.kill_threshold == 85.0
    assert audit_node().deterministic_gate.test_command == AUDIT_TEST_COMMAND


# ---------------------------------------- edge cases of the verdict and the copy


@pytest.mark.parametrize(
    "noise_dir", [".pytest_cache", ".hypothesis", ".ruff_cache", ".mutmut-cache", "mutants"]
)
def test_every_listed_noise_directory_is_not_a_change(tmp_path: Path, noise_dir: str) -> None:
    tree = tmp_path / "tree"
    _init(tree, {"n.py": BASE_CODE, "test_n.py": TEST_BODY.format(value=1)})
    (tree / noise_dir).mkdir()
    (tree / noise_dir / "x").write_text("noise\n")
    assert audit_tree(tree).verdict == "nothing-to-audit"


def test_the_result_tree_is_the_staged_copys_write_tree(clean_tree: Path, tmp_path: Path) -> None:
    """`tree` names what was judged: the write-tree with the untracked test staged."""
    twin = tmp_path / "twin"
    shutil.copytree(clean_tree, twin)
    _git(twin, "add", "-A")
    expected = run_capture(["git", "write-tree"], twin).stdout.strip()
    result = audit_tree(clean_tree)
    assert result.tree == expected
    assert result.tree != result.baseline


def test_a_check_keeps_the_basis_the_gate_gave_it(clean_tree: Path) -> None:
    result = audit_tree(clean_tree)
    by_name = {check.name: check for check in result.checks}
    assert result.mutation is not None
    assert by_name["mutation"].basis == f"sampled n={result.mutation.total}"
    assert by_name["coverage"].basis == "changed-lines=4"


def test_a_custom_test_command_is_the_one_gated_and_reported(clean_tree: Path) -> None:
    command = "python -m pytest -q -k no_such_test"  # collects nothing: exit 5
    result = audit_tree(clean_tree, test_command=command)
    failing = {check.name for check in result.checks if check.status == "fail"}
    assert result.test_command == command
    assert result.verdict == "refuse"
    assert "tests" in failing


def test_the_recorder_is_threaded_into_the_gate(clean_tree: Path, tmp_path: Path) -> None:
    journal = tmp_path / "proofs.jsonl"
    audit_tree(clean_tree, recorder=SpanRecorder(path=journal, node_id="audit"))
    assert [span for span in read_spans(journal) if span.kind == "tool"]


# ---------------------------------------- the audit's verdict cache


def _the_only_cache_file(cache: Path) -> Path:
    [cache_file] = list(cache.iterdir())
    return cache_file


def test_cache_hit_skips_gates_and_returns_the_same_verdict(
    clean_tree: Path, tmp_path: Path
) -> None:
    """Pair 1 (key): a hit skips the gates and returns the same verdict."""
    cache = tmp_path / "cache"
    journal_1 = tmp_path / "first.jsonl"
    first = audit_tree(
        clean_tree, cache=cache, recorder=SpanRecorder(path=journal_1, node_id="audit")
    )
    assert first.cached is False
    assert read_spans(journal_1)

    journal_2 = tmp_path / "second.jsonl"
    second = audit_tree(
        clean_tree, cache=cache, recorder=SpanRecorder(path=journal_2, node_id="audit")
    )
    assert second.cached is True
    assert read_spans(journal_2) == []
    assert second.to_dict() == {**first.to_dict(), "cached": True}


def test_one_byte_of_source_is_a_miss(clean_tree: Path, tmp_path: Path) -> None:
    """Pair 2: a single changed byte of the audited tree's source is a miss."""
    cache = tmp_path / "cache"
    audit_tree(clean_tree, cache=cache)
    # One trailing space: same statement, same behaviour, one different byte.
    (clean_tree / "n.py").write_text(FIXED_CODE.replace("return 2", "return 2 "))
    journal = tmp_path / "second.jsonl"
    result = audit_tree(
        clean_tree, cache=cache, recorder=SpanRecorder(path=journal, node_id="audit")
    )
    assert result.cached is False
    assert read_spans(journal)


def test_a_different_gate_surface_is_a_miss(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 3: a different gate surface is a miss (M-K1)."""
    cache = tmp_path / "cache"
    audit_tree(clean_tree, cache=cache)
    monkeypatch.setattr(audit, "gate_surface", lambda *a, **kw: "other")
    result = audit_tree(clean_tree, cache=cache)
    assert result.cached is False


def test_a_different_test_command_is_a_miss(clean_tree: Path, tmp_path: Path) -> None:
    """Pair 4: a different test command is a miss (M-K2)."""
    cache = tmp_path / "cache"
    audit_tree(clean_tree, cache=cache)
    result = audit_tree(clean_tree, cache=cache, test_command="python -m pytest -q test_n.py")
    assert result.cached is False


def test_a_planted_verdict_is_not_served(untracked_module_tree: Path, tmp_path: Path) -> None:
    """Pair 5: a planted verdict, filed under a tampered key, is not served (M-K3)."""
    cache = tmp_path / "cache"
    first = audit_tree(untracked_module_tree, cache=cache)
    assert first.verdict == "refuse"
    cache_file = _the_only_cache_file(cache)
    payload = json.loads(cache_file.read_text())
    payload["key"]["tree"] = "0" * 40
    payload["result"]["verdict"] = "accept"
    cache_file.write_text(json.dumps(payload))

    result = audit_tree(untracked_module_tree, cache=cache)
    assert result.verdict == "refuse"
    assert result.cached is False


def test_garbage_cache_file_is_a_miss_not_a_crash(clean_tree: Path, tmp_path: Path) -> None:
    """Pair 6: unparseable cache content is a miss, never an exception (M-K4)."""
    cache = tmp_path / "cache"
    audit_tree(clean_tree, cache=cache)
    _the_only_cache_file(cache).write_bytes(b"{not json")

    result = audit_tree(clean_tree, cache=cache)
    assert result.cached is False


def test_gate_surface_moves_with_each_input_and_nothing_else(tmp_path: Path) -> None:
    """Pair 7: `gate_surface` moves with each input and nothing else (M-K5)."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n")
    b.write_text("y = 2\n")
    versions = {"mutmut": "1.0", "ruff": "2.0", "coverage": "3.0"}

    base = gate_surface([a, b], versions)
    assert gate_surface([a, b], dict(versions)) == base

    a.write_text("x = 11\n")
    assert gate_surface([a, b], versions) != base
    a.write_text("x = 1\n")
    assert gate_surface([a, b], versions) == base

    assert gate_surface([a, b], {**versions, "ruff": "2.1"}) != base
    assert gate_surface([b, a], versions) != base


def test_a_tracked_coveragerc_is_not_noise(tmp_path: Path) -> None:
    """Pair 8 (COPY_IGNORE): a tracked `.coveragerc` is not noise (M-K6).

    Red at 29dc661: `shutil.ignore_patterns(".coverage*", ...)` also drops
    `.coveragerc`, so the copy loses a tracked file the baseline has and an
    unchanged repo audits as `refuse`, not `nothing-to-audit`.
    """
    tree = tmp_path / "tree"
    _init(
        tree,
        {
            ".coveragerc": "[run]\nbranch = True\n",
            "n.py": BASE_CODE,
            "test_n.py": TEST_BODY.format(value=1),
        },
    )
    assert audit_tree(tree).verdict == "nothing-to-audit"


def test_tracked_bytecode_is_not_noise(tmp_path: Path) -> None:
    """A tracked `__pycache__/*.pyc` is not noise.

    Dropping `__pycache__` and `*.pyc` at any depth by name alone would leave
    the copy without a file the baseline tracks; git would read it as deleted,
    and an unchanged repository would audit as `refuse`.
    """
    tree = tmp_path / "tree"
    _init(
        tree,
        {
            "n.py": BASE_CODE,
            "test_n.py": TEST_BODY.format(value=1),
            "__pycache__/n.cpython-312.pyc": "not real bytecode\n",
        },
    )
    assert audit_tree(tree).verdict == "nothing-to-audit"


def test_audit_ignore_tells_tracked_and_untracked_siblings_apart(tmp_path: Path) -> None:
    """One directory, a tracked and an untracked `.pyc`. The directory is kept
    because a tracked file lives beneath it (the ancestor set), the untracked
    sibling is still dropped, the tracked one is kept, and an untracked
    `__pycache__` elsewhere is still dropped."""
    tree = tmp_path / "tree"
    _init(tree, {"pkg/__init__.py": "", "pkg/__pycache__/a.pyc": "a"})
    (tree / "pkg" / "__pycache__" / "b.pyc").write_bytes(b"b")
    (tree / "other" / "__pycache__").mkdir(parents=True)
    (tree / "other" / "__pycache__" / "c.pyc").write_bytes(b"c")
    ignore = audit._audit_ignore(tree)
    assert ignore(str(tree / "pkg"), ["__init__.py", "__pycache__"]) == set()
    assert ignore(str(tree / "pkg" / "__pycache__"), ["a.pyc", "b.pyc"]) == {"b.pyc"}
    assert ignore(str(tree / "other"), ["__pycache__"]) == {"__pycache__"}
    assert ignore(str(tree / "other" / "__pycache__"), ["c.pyc"]) == {"c.pyc"}


def test_a_git_directory_git_cannot_read_is_an_audit_error(tmp_path: Path) -> None:
    """`git ls-files` failing is a named error, not a silently empty tracked set
    that would drop tracked bytecode again."""
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    with pytest.raises(AuditError, match="git ls-files failed"):
        audit_tree(tree)


def test_mutants_and_halter_below_top_level_are_real_files(tmp_path: Path) -> None:
    """Pair 9: `mutants`/`.halter` below the top level are real, tracked files (M-K7)."""
    tree = tmp_path / "tree"
    _init(
        tree,
        {
            "pkg/mutants/__init__.py": "x = 1\n",
            "pkg/.halter/keep.txt": "keep\n",
        },
    )
    assert audit_tree(tree).verdict == "nothing-to-audit"


def test_a_matching_key_over_a_malformed_result_is_a_miss(clean_tree: Path, tmp_path: Path) -> None:
    """A parseable cache file whose key matches but whose result cannot be
    rebuilt is a miss, never an exception (`from_dict` raising `KeyError`
    must not escape)."""
    cache = tmp_path / "cache"
    audit_tree(clean_tree, cache=cache)
    stored = _the_only_cache_file(cache)
    data = json.loads(stored.read_text())
    data["result"] = {"verdict": "accept"}
    stored.write_text(json.dumps(data))

    result = audit_tree(clean_tree, cache=cache)
    assert result.cached is False


# ------------------------------- results name real paths, not the copy


def _surviving_mutmut(stub_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutmut stub whose one changed-line mutant survives the tree's tests."""
    stub_dir.mkdir()
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        "  results) echo '  m1: survived';;\n"
        "  show) printf -- '--- n.py\\n+++ n.py\\n@@ -2 +2 @@\\n"
        "-    return 2\\n+    return 3\\n';;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


def test_a_check_detail_names_the_trees_file_not_the_temporary_copys(
    untracked_module_tree: Path,
) -> None:
    """Contract A, pair 1: the coverage detail says `m.py:1`, never `<tmp>/tree/m.py:1`."""
    result = audit_tree(untracked_module_tree)
    coverage = next(check for check in result.checks if check.name == "coverage")
    assert "m.py:" in coverage.detail
    assert "/m.py" not in coverage.detail


def test_survivor_lines_are_spelled_relative_to_the_tree(
    clean_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract A, pair 2: a survivor's path has no directory part."""
    _surviving_mutmut(tmp_path / "stub", monkeypatch)
    result = audit_tree(clean_tree)
    assert result.mutation is not None
    assert result.mutation.survivor_lines == (("n.py", 2),)


def test_two_uncached_audits_of_one_tree_are_byte_identical(
    untracked_module_tree: Path,
) -> None:
    """Contract A, pair 3: the temporary directory's name reaches no result byte."""
    first = audit_tree(untracked_module_tree)
    second = audit_tree(untracked_module_tree)
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_rewriting_paths_moves_no_verdict_status_or_plan_reason(
    untracked_module_tree: Path,
) -> None:
    """Contract A, pair 4 (known-good): a detail that never named the copy is untouched."""
    result = audit_tree(untracked_module_tree)
    assert result.verdict == "refuse"
    statuses = {check.name: check.status for check in result.checks}
    assert statuses["coverage"] == "fail"
    assert statuses["tests"] == "pass"
    for name, reason in NOT_APPLICABLE.items():
        check = next(check for check in result.checks if check.name == name)
        assert (check.status, check.detail) == ("not-applicable", reason)


def test_a_basis_that_names_the_copy_is_spelled_from_the_root(tmp_path: Path) -> None:
    """Contract A covers `basis` too. No gate puts a path there today (every
    basis is a count), so this pins the helper directly rather than through
    a gate: known-bad `<copy>/n.py` in a basis, known-good a `None` basis and
    a basis that never named the copy."""
    copy = tmp_path / "tree"
    checks = (
        audit.AuditCheck("a", "pass", f"see {copy}/n.py:2", f"from {copy}/n.py"),
        audit.AuditCheck("b", "pass", "clean", None),
        audit.AuditCheck("c", "pass", "clean", "changed-lines=1"),
    )
    spelled, mutation = audit._spelled_from_the_root(checks, None, copy)
    assert [(c.detail, c.basis) for c in spelled] == [
        ("see n.py:2", "from n.py"),
        ("clean", None),
        ("clean", "changed-lines=1"),
    ]
    assert mutation is None


# ------------------------------- halter H2-3: contract mutants (b) and (c)


def _mutmut_reporting(stub_dir: Path, monkeypatch: pytest.MonkeyPatch, verdicts: str) -> None:
    """A mutmut stub whose `results` line is `verdicts`, every mutant on n.py:2."""
    stub_dir.mkdir()
    script = stub_dir / "mutmut"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{verdicts}';;\n"
        "  show) printf -- '--- n.py\\n+++ n.py\\n@@ -2 +2 @@\\n"
        "-    return 2\\n+    return 3\\n';;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def tests_unchanged_tree(tmp_path: Path) -> Path:
    """`n.py` changes, its (tracked, passing) test does not: red-phase binds."""
    tree = tmp_path / "tree"
    _init(
        tree,
        {
            "n.py": BASE_CODE,
            "test_n.py": "from n import f\n\n\ndef test_f():\n    assert f() > 0\n",
        },
    )
    (tree / "n.py").write_text(FIXED_CODE)
    return tree


@pytest.mark.parametrize(
    ("verdicts", "status"),
    [
        ("  m1: killed\n  m2: killed\n  m3: killed\n  m4: killed\n  m5: killed", "pass"),
        ("  m1: killed\n  m2: killed\n  m3: killed\n  m4: killed\n  m5: survived", "fail"),
    ],
)
def test_red_phase_with_tests_unchanged_needs_85_percent_killed(
    tests_unchanged_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdicts: str,
    status: str,
) -> None:
    """Contract (b): 5/5 killed carries the proof; 4/5 (80%) is under the 85% floor.

    The floor is `REFACTOR_KILL_FLOOR`, fixed in the gate and not taken from
    any input: a mutant that lowers it to 0 lets the 80% tree pass red-phase.
    """
    _mutmut_reporting(tmp_path / "stub", monkeypatch, verdicts)
    result = audit_tree(tests_unchanged_tree)
    red = next(check for check in result.checks if check.name == "red-phase")
    assert red.status == status, red.detail
    if status == "fail":
        assert "80.0% < 85.0%" in red.detail
        assert result.verdict == "refuse"
    else:
        assert result.verdict == "accept", [
            (c.name, c.detail) for c in result.checks if c.status == "fail"
        ]


@pytest.mark.parametrize("tool", ["mutmut", "ruff", "coverage"])
def test_default_gate_surface_moves_with_each_tools_installed_version(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """Contract (c): the no-argument surface asks the installed version of every
    tool in `SURFACE_TOOLS`; a different answer for any one of them is a
    different surface, and an unrelated distribution's version is not."""
    import importlib.metadata

    real_version = importlib.metadata.version
    base = gate_surface()
    assert gate_surface() == base

    def other(name: str) -> str:
        return "0.0.0-other" if name == tool else real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", other)
    assert gate_surface() != base

    def unrelated(name: str) -> str:
        return "0.0.0-other" if name == "pip" else real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", unrelated)
    assert gate_surface() == base


@pytest.mark.parametrize(
    "module",
    [
        "halter.audit",
        "halter.auditor",
        "halter.runner",
        "halter.gates",
        "halter.evidence",
        "halter.dag",
    ],
)
def test_default_gate_surface_moves_with_each_check_modules_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    """The no-argument surface hashes every module whose bytes decide a
    verdict, the tiered battery's included: one edited byte in any of them
    is a different surface, so no cached verdict outlives the code that
    reached it. The list is spelled out here, not read from SURFACE_MODULES,
    so dropping a module from that tuple is a failure, not a smaller test."""
    import importlib

    target = importlib.import_module(module)
    base = gate_surface()
    edited = tmp_path / "edited.py"
    edited.write_bytes(Path(str(target.__file__)).read_bytes() + b"\n# one more line\n")
    monkeypatch.setattr(target, "__file__", str(edited))
    assert gate_surface() != base
