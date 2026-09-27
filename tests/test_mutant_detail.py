"""`mutant_detail`: (name, status, show) recorded for every scored mutant.

`evidence.mutation_sample` keeps the `mutmut show` text it already reads for
each mutant in `total`, killed ones included; a mutant mutmut never scored
(`not checked`) has none. The tiered battery's tier-2 `Findings` carry it
and the verdict cache stores it as `{name, status, show}` rows. No verdict
reads it, and neither `--json` report prints it, so both stay what they
were. There is no killing-test field: nothing records which test killed a
mutant.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from halter.auditor import Auditor, AuditorConfig, Findings
from halter.cli import main
from halter.evidence import mutation_sample, run_capture


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
SHOW3 = "--- n.py\n+++ n.py\n@@ -3 +3 @@\n-    return 2\n+    return 3\n"


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


def test_mutant_detail_records_killed_mutants_and_omits_unscored_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = "  m1: survived\n  m2: killed\n  m3: no tests\n  m4: not checked"
    _stub(tmp_path, monkeypatch, results, SHOW)
    work = tmp_path / "w"
    work.mkdir()
    (work / "n.py").write_text("def f():\n    return 2\n")
    outcome = mutation_sample(work, {(str(work / "n.py"), 2)}, 10, test_files=())
    assert [(n, s) for n, s, _ in outcome.mutant_detail] == [
        ("m1", "survived"),
        ("m2", "killed"),
        ("m3", "no tests"),
    ]
    assert {n: t for n, _, t in outcome.mutant_detail}["m2"] == SHOW


def test_tier2_findings_carry_mutant_detail_and_round_trip(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    auditor = Auditor(tree)
    found = auditor.tier2()
    assert [(n, s) for n, s, _ in found.mutant_detail] == [("k1", "killed"), ("s1", "survived")]
    assert found.to_dict()["mutant_detail"][0] == {"name": "k1", "status": "killed", "show": SHOW3}
    assert Findings.from_dict(found.to_dict()).mutant_detail == found.mutant_detail
    assert auditor.tier1().mutant_detail == ()
    assert "mutant_detail" not in auditor.tier1().to_dict()


def test_the_verdict_cache_keeps_mutant_detail_and_a_cache_hit_returns_it(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    cache = tmp_path / "cache"
    fresh = Auditor(tree, config=AuditorConfig(cache_dir=cache)).tier2()
    stored = json.loads((cache / f"{fresh.key}.json").read_text())
    assert [d["name"] for d in stored["mutant_detail"]] == ["k1", "s1"]
    hit = Auditor(tree, config=AuditorConfig(cache_dir=cache)).tier2()
    assert hit.cached
    assert hit.mutant_detail == fresh.mutant_detail


@pytest.mark.parametrize("tiered", [True, False], ids=["tiered", "default"])
def test_json_reports_leave_mutant_detail_out(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tiered: bool
) -> None:
    _stub(tmp_path, monkeypatch, "  k1: killed\n  s1: survived", SHOW3)
    out = io.StringIO()
    argv = ["--no-cache", "--json", "--repo", str(tree), *(["--tiered"] if tiered else [])]
    main(argv, stdout=out, stderr=io.StringIO())
    report = json.loads(out.getvalue())
    if tiered:
        assert report["tiers"][-1]["tier"] == 2
    else:
        assert report["mutation"]["total"] == 2
    assert "mutant_detail" not in out.getvalue()
