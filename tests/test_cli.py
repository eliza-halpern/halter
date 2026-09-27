"""Tests for halter.cli: the command's exit codes, report and modes."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from halter.cli import build_parser, main

AUDIT_BASE_CODE = "def f():\n    return 1\n"
AUDIT_FIXED_CODE = "def f():\n    return 2\n"
AUDIT_TEST_BODY = "from n import f\n\n\ndef test_f():\n    assert f() == {value}\n"


def _audit_git(root: Path, *argv: str) -> str:
    run = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=True)
    return run.stdout.strip()


def _audit_commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (root / name).write_text(text)
    _audit_git(root, "add", "-A")
    _audit_git(root, "commit", "-m", message)
    return _audit_git(root, "rev-parse", "HEAD")


def _audit_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _audit_git(root, "init")
    _audit_git(root, "config", "user.email", "test@example.com")
    _audit_git(root, "config", "user.name", "test")
    _audit_commit(root, files, "baseline")
    return root


@pytest.fixture
def audit_clean(tmp_path: Path) -> Path:
    """An accepted change: `n.py` fixed, its test new and untracked."""
    root = _audit_repo(tmp_path / "clean", {"n.py": AUDIT_BASE_CODE})
    (root / "n.py").write_text(AUDIT_FIXED_CODE)
    (root / "test_n.py").write_text(AUDIT_TEST_BODY.format(value=2))
    return root


@pytest.fixture
def audit_untracked_module(tmp_path: Path) -> Path:
    """A tested change plus an untracked `m.py` no test imports: coverage refuses."""
    root = _audit_repo(
        tmp_path / "refuse",
        {"n.py": AUDIT_BASE_CODE, "test_n.py": AUDIT_TEST_BODY.format(value=1)},
    )
    (root / "n.py").write_text(AUDIT_FIXED_CODE)
    (root / "test_n.py").write_text(AUDIT_TEST_BODY.format(value=2))
    (root / "m.py").write_text("def g():\n    return 7\n")
    return root


@pytest.fixture
def audit_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """c1 -> c2 is a tested change; the working tree adds an uncommitted, untested `m.py`.

    c1 has no test: editing an existing assertion would trip assertion-preservation,
    and this fixture must be accepted for the stated reason only.
    """
    root = _audit_repo(tmp_path / "history", {"n.py": AUDIT_BASE_CODE})
    c1 = _audit_git(root, "rev-parse", "HEAD")
    c2 = _audit_commit(
        root,
        {"n.py": AUDIT_FIXED_CODE, "test_n.py": AUDIT_TEST_BODY.format(value=2)},
        "fix",
    )
    (root / "m.py").write_text("def g():\n    return 7\n")
    return root, c1, c2


def _audit_main(
    tmp_path: Path, repo: Path, *extra: str, cache: bool = True
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    argv = [*extra, "--repo", str(repo)]
    if cache:
        argv += ["--cache", str(tmp_path / "cache")]
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _check_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith(("PASS", "FAIL", "n/a"))]


def test_audit_needs_no_api_key(
    audit_clean: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HALTER_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    code, out, err = _audit_main(tmp_path, audit_clean)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")


def test_audit_accept_prints_the_battery(audit_clean: Path, tmp_path: Path) -> None:
    code, out, _err = _audit_main(tmp_path, audit_clean)
    assert code == 0
    assert out.rstrip().splitlines()[-1] == "verdict: accept"
    lines = _check_lines(out)
    assert len(lines) == 13
    assert len([ln for ln in lines if ln.startswith("n/a")]) == 4
    header = out.splitlines()[0]
    assert header.endswith("fresh")
    assert not header.startswith(("PASS", "FAIL", "n/a"))


def test_audit_refuse_is_exit_one_with_the_failing_check(
    audit_untracked_module: Path, tmp_path: Path
) -> None:
    code, out, _err = _audit_main(tmp_path, audit_untracked_module)
    assert code == 1
    assert out.rstrip().endswith("verdict: refuse")
    coverage = [ln for ln in _check_lines(out) if "coverage" in ln.split()[1:2]]
    assert len(coverage) == 1
    assert coverage[0].startswith("FAIL")


def test_audit_nothing_to_audit_is_exit_three(tmp_path: Path) -> None:
    root = _audit_repo(tmp_path / "same", {"n.py": AUDIT_BASE_CODE})
    code, out, _err = _audit_main(tmp_path, root)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    assert _check_lines(out) == []


def test_audit_json_is_exactly_the_result_dict(audit_clean: Path, tmp_path: Path) -> None:
    code, out, _err = _audit_main(tmp_path, audit_clean, "--json")
    assert code == 0
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(out)
    assert out[end:].strip() == ""
    assert value["verdict"] == "accept"
    assert set(value) == {
        "verdict",
        "tree",
        "baseline",
        "test_command",
        "checks",
        "mutation",
        "surface",
        "cached",
    }


def test_audit_rev_mode_ignores_the_working_tree(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, c1, c2 = audit_two_commits
    before = _audit_git(root, "status", "--porcelain")
    # The fixture can tell the modes apart: the working tree against c1 is refused.
    tree_code, _out, _err = _audit_main(tmp_path, root, "--baseline", c1)
    assert tree_code == 1
    code, out, err = _audit_main(tmp_path, root, c2)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")
    assert _audit_git(root, "status", "--porcelain") == before
    assert c1[:12] in out.splitlines()[0]


def test_audit_rev_mode_takes_an_explicit_baseline(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    root, c1, c2 = audit_two_commits
    code, out, _err = _audit_main(tmp_path, root, c2, "--baseline", c2)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    assert c1[:12] not in out


def test_audit_unknown_revision_is_exit_two(audit_clean: Path, tmp_path: Path) -> None:
    code, out, err = _audit_main(tmp_path, audit_clean, "no-such-rev")
    assert code == 2
    assert out == ""
    assert err.startswith("error: ")
    assert "no-such-rev" in err


def test_audit_unknown_baseline_is_exit_two(audit_clean: Path, tmp_path: Path) -> None:
    code, _out, err = _audit_main(tmp_path, audit_clean, "--baseline", "no-such-base")
    assert code == 2
    assert "no-such-base" in err


def test_audit_a_non_repository_is_exit_two(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    for extra in ((), ("HEAD",)):
        code, out, err = _audit_main(tmp_path, plain, *extra)
        assert (code, out) == (2, "")
        assert err.startswith("error: ")


def test_audit_no_cache_writes_no_cache(audit_clean: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    code, _out, _err = _audit_main(tmp_path, audit_clean, "--no-cache", cache=False)
    assert code == 0
    assert not cache.exists()


def test_audit_cache_serves_the_second_run(audit_clean: Path, tmp_path: Path) -> None:
    _audit_main(tmp_path, audit_clean)
    assert (tmp_path / "cache").is_dir()
    code, out, _err = _audit_main(tmp_path, audit_clean)
    assert code == 0
    assert out.splitlines()[0].endswith("cached")


def test_audit_parser_defaults() -> None:
    from halter import audit as audit_module

    args = build_parser().parse_args([])
    assert (args.rev, args.repo, args.baseline, args.json, args.no_cache) == (
        None,
        ".",
        None,
        False,
        False,
    )
    assert args.test_command == audit_module.AUDIT_TEST_COMMAND
    assert args.cache == audit_module.DEFAULT_AUDIT_CACHE
    assert args.tiered is False


# --- option plumbing ---------------------------------------------------------


def test_audit_test_command_reaches_the_audit_in_both_modes(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    """`--test-command` is the audit's command, working-tree or REV mode."""
    root, c1, c2 = audit_two_commits
    command = "python -m pytest -q -p no:cacheprovider"
    for extra in (("--baseline", c1), (c2,)):
        code, out, _err = _audit_main(tmp_path, root, *extra, "--test-command", command, "--json")
        assert json.loads(out)["test_command"] == command
        assert code in (0, 1)


def test_audit_rev_mode_uses_the_cache(
    audit_two_commits: tuple[Path, str, str], tmp_path: Path
) -> None:
    """The verdict cache serves a REV-mode rerun (its key holds no path)."""
    root, _c1, c2 = audit_two_commits
    first = _audit_main(tmp_path, root, c2)
    second = _audit_main(tmp_path, root, c2)
    assert first[0] == second[0] == 0
    assert first[1].splitlines()[0].endswith("fresh")
    assert second[1].splitlines()[0].endswith("cached")


def test_audit_rev_mode_accepts_a_relative_repo(
    audit_two_commits: tuple[Path, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--repo .` (the default) must survive the clone running elsewhere."""
    root, _c1, c2 = audit_two_commits
    monkeypatch.chdir(root)
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [c2, "--repo", ".", "--cache", str(tmp_path / "cache")],
        stdout=out,
        stderr=err,
    )
    assert (code, err.getvalue()) == (0, "")
    assert out.getvalue().rstrip().endswith("verdict: accept")


# --- --tiered (ported from saddle tests/test_auditor.py at 4a4b60d) ---


def _tiered(repo: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--tiered", "--no-cache", *extra, "--repo", str(repo)], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def audit_uncovered(audit_clean: Path) -> Path:
    """`audit_clean` plus an untracked `m.py` no test runs: tier 1's coverage fails."""
    (audit_clean / "m.py").write_text("def g():\n    return 7\n")
    return audit_clean


def test_tiered_accepts_a_correct_change(audit_clean: Path) -> None:
    code, out, err = _tiered(audit_clean)
    assert (code, err) == (0, "")
    assert out.rstrip().endswith("verdict: accept")
    assert "tier 2" in out
    assert "[evidence-thin]" in out


def test_tiered_refuses_with_exit_one_and_json(audit_uncovered: Path) -> None:
    code, out, _err = _tiered(audit_uncovered, "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["verdict"] == "refuse"
    assert [t["tier"] for t in payload["tiers"]] == [0, 0, 0, 1, 2]
    assert [t["passed"] for t in payload["tiers"]] == [True, True, True, False, False]
    blocked = payload["tiers"][-1]["findings"]
    assert [(f["gate"], f["verdict"], f["reason"]) for f in blocked] == [
        ("mutation", "blocked", "unknown")
    ]
    assert blocked[0]["detail"] == "tier 1 failed (coverage); tier 2 not run"


def test_tiered_text_report_is_one_line_per_finding(audit_uncovered: Path) -> None:
    code, out, _err = _tiered(audit_uncovered)
    assert code == 1
    lines = out.splitlines()
    assert [ln.split()[:2] for ln in lines if ln.startswith("tier ")] == [
        ["tier", "0"],
        ["tier", "0"],
        ["tier", "0"],
        ["tier", "1"],
        ["tier", "2"],
    ]
    assert any(ln.split()[:3] == ["fail", "coverage", "[evidence-thin]"] for ln in lines)
    assert lines[-2].split()[:3] == ["blocked", "mutation", "[unknown]"]
    assert lines[-1] == "verdict: refuse"


def test_tiered_rev_mode_and_exit_codes(audit_clean: Path) -> None:
    _audit_git(audit_clean, "add", "-A")
    _audit_git(audit_clean, "commit", "-m", "fix")
    code, out, _err = _tiered(audit_clean, "HEAD")
    assert code == 0, out
    code, out, _err = _tiered(audit_clean)
    assert code == 3
    assert out.rstrip().endswith("verdict: nothing to audit")
    code, _out, err = _tiered(audit_clean, "--baseline", "no-such-base")
    assert code == 2
    assert "no-such-base" in err


def test_version_is_one_string_everywhere_it_is_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Contract: `halter --version` prints the version pyproject.toml ships,
    and CITATION.cff records the same one. A release that bumps one and not
    the others would publish a wheel that misreports itself."""
    import re
    import tomllib

    root = Path(__file__).resolve().parent.parent
    shipped = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    cited = re.search(r'^version: "([^"]+)"$', (root / "CITATION.cff").read_text(), re.M)
    assert cited is not None
    assert cited.group(1) == shipped
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"halter {shipped}"
