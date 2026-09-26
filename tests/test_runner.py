"""Tests for halter.runner's `tier2` switch: the checkpoint run measures less.

`tier2=False` is what the tiered battery's tier 1 runs; the default is the
full battery `audit_tree` has always run. Fixtures follow test_audit.py: the
changed source line is `    return 2`, on which the conftest's `mutmut` stub
reports five killed mutants.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from halter import runner
from halter.audit import audit_node
from halter.evidence import run_capture
from halter.gates import RED_PHASE_SAMPLES

BASE_CODE = "def f():\n    return 1\n"
FIXED_CODE = "def f():\n    return 2\n"
TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"


def _git(root: Path, *argv: str) -> None:
    assert run_capture(["git", *argv], root).exit_code == 0


@pytest.fixture
def staged_tree(tmp_path: Path) -> Path:
    """A committed `return 1`, then `return 2` and a new test, both staged."""
    tree = tmp_path / "tree"
    tree.mkdir()
    _git(tree, "init")
    _git(tree, "config", "user.email", "test@example.com")
    _git(tree, "config", "user.name", "test")
    (tree / "n.py").write_text(BASE_CODE)
    _git(tree, "add", "-A")
    _git(tree, "commit", "-m", "baseline")
    (tree / "n.py").write_text(FIXED_CODE)
    (tree / "test_n.py").write_text(TEST_BODY.format(value=2))
    _git(tree, "add", "-A")
    return tree


class _Spy:
    """Counts calls through to the wrapped function."""

    def __init__(self, target: Callable[..., object]) -> None:
        self.target = target
        self.calls = 0
        self.args: list[tuple[object, ...]] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        self.args.append(args)
        return self.target(*args, **kwargs)


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> _Spy:
    spy = _Spy(getattr(runner, name))
    monkeypatch.setattr(runner, name, spy)
    return spy


def test_tier2_false_measures_no_mutation(
    staged_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutation = _spy(monkeypatch, "mutation_sample")
    gated = runner.run_node_gate(audit_node(), staged_tree, tier2=False)
    assert mutation.calls == 0
    assert gated.mutation is not None
    assert gated.mutation.survivors == (runner.NOT_MEASURED_AT_TIER1,)
    full = runner.run_node_gate(audit_node(), staged_tree)
    assert mutation.calls == 1
    assert full.mutation is not None
    assert full.mutation.killed == 5


def test_tier2_false_takes_one_red_phase_sample_not_all(
    staged_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each baseline sample drops the caches of the baseline copy before it
    # runs; the one other call per run is on the tree itself.
    drops = _spy(monkeypatch, "drop_test_caches")

    def baseline_samples() -> int:
        count = sum(1 for args in drops.args if Path(str(args[0])) != staged_tree)
        drops.args.clear()
        return count

    runner.run_node_gate(audit_node(), staged_tree, tier2=False)
    assert baseline_samples() == 1
    runner.run_node_gate(audit_node(), staged_tree)
    assert baseline_samples() == RED_PHASE_SAMPLES
