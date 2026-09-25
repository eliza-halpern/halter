"""Shared pytest fixtures: hermetic cwd and a hermetic mutmut for every test.

A mutant that drops a `cwd` argument (a `run_capture(argv, None)`) makes
git inherit pytest's cwd. Running each test from a disposable directory
keeps those side effects out of the checkout.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from halter import evidence
from halter.journal import SpanRecorder

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _empty_cwd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test with cwd in a dedicated empty tmp dir."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


def _replay_show_all_mutants(
    scratch: Path, *, recorder: SpanRecorder | None = None
) -> dict[str, str]:
    """Stand-in for `evidence.show_all_mutants` under a PATH-stub `mutmut`.

    The PATH-stub `mutmut` scripts never write a `mutants/` directory, so the
    production lookup subprocess -- which reads mutmut's own meta files --
    would find nothing under them. This replays the old per-name loop
    instead: `mutmut results --all True`, then `mutmut show NAME` for every
    name the results line, through whichever `mutmut` is first on PATH.
    `subprocess.run` is used directly, not `evidence.run_capture`, so a test
    that patches `run_capture` never observes these calls.
    """
    results = subprocess.run(
        ["mutmut", "results", "--all", "True"], cwd=scratch, capture_output=True, text=True
    )
    names = re.findall(r"^\s*(\S+): ", results.stdout, re.MULTILINE)
    mapping: dict[str, str] = {}
    for name in names:
        shown = subprocess.run(
            ["mutmut", "show", name], cwd=scratch, capture_output=True, text=True
        )
        mapping[name] = shown.stdout
    return mapping


@pytest.fixture(autouse=True)
def _stub_mutmut(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic mutmut: e2e tests exercise the collector without real runs.

    Reports MIN_SIGNIFICANT_MUTANTS killed mutants that locate to the
    line every worktree fixture changes (n.py:2), so a tree with healthy
    tests passes the mutation check for the stated reason. Tests needing
    other verdicts override PATH.

    `show_all_mutants` is patched to `_replay_show_all_mutants`: the
    production lookup shells out to a real mutmut installation's meta files,
    which this stub never creates.
    """
    stub_dir = tmp_path_factory.mktemp("mutmut-stub")
    script = stub_dir / "mutmut"
    results = "\n".join(f"  m{index}: killed" for index in range(1, 6))
    show = "--- n.py\n+++ n.py\n@@ -2 +2 @@\n-    return 2\n+    return 3\n"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  run) exit 0;;\n"
        f"  results) printf '%s\\n' '{results}';;\n"
        f"  show) printf '%s' '{show}';;\n"
        "esac\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(evidence, "show_all_mutants", _replay_show_all_mutants)
