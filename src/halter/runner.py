"""Run every check over one tree: a working directory and a declaration in, a verdict out.

`run_node_gate` is straight-line glue over `halter.evidence` collectors
and `halter.gates` predicates: one coverage-wrapped suite run serves both
the tests check and the coverage check, plus the baseline runs for
red-phase. The change must be tracked (staged or committed) so the
baseline diff sees it; `audit_tree` stages the tree before calling in.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePath

from halter.dag import Node
from halter.evidence import (
    CapturedRun,
    MutationOutcome,
    changed_statements,
    covered_lines,
    drop_test_caches,
    git_added_files,
    git_changed_files,
    git_diff,
    materialize_baseline,
    mutation_sample,
    property_modules,
    pytest_scope,
    ruff_argv,
    ruff_findings,
    run_capture,
    run_shell_capture,
    scoped_targets,
    under_coverage,
)
from halter.gates import (
    RED_PHASE_SAMPLES,
    Tier1Inputs,
    Tier1Result,
    introduced_findings,
    run_tier1,
)
from halter.journal import SpanRecorder

# The placeholder survivor a `tier2=False` run carries in place of a mutation
# sample: never a verdict, only a marker that nothing was measured.
NOT_MEASURED_AT_TIER1 = "not measured: tier-1 checkpoint"


def read_sources(root: Path, pattern: str) -> dict[str, str]:
    """Map workdir-relative posix paths to text for files matching `pattern`."""
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob(pattern))
        if path.is_file()
    }


class _Stubber(ast.NodeTransformer):
    """Replace every function body with `raise NotImplementedError`."""

    def _empty(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = [ast.Raise(exc=ast.Name(id="NotImplementedError", ctx=ast.Load()), cause=None)]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._empty(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._empty(node)


def _stub_module(source: str) -> str:
    """A signature-preserving stub of `source`: same API, no behaviour.

    Red-phase accepts a baseline collection error for a module the change
    creates, because that module cannot be imported before it exists. On
    its own that acceptance is satisfiable by any new test of any new
    module: the import error names a changed source, and the check would
    pass whatever the test asserts.

    Materializing a stub instead gives the pre-change run something to
    import. A test that exercises the new code then fails for a real
    reason, and a tautological one passes pre-change and is rejected --
    which is what the check is for.
    """
    tree = _Stubber().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _test_signatures(sources: dict[str, str]) -> dict[str, str]:
    """Map each test module to a comment- and layout-insensitive signature.

    Whether red-phase binds is read off the diff, so the comparison must
    ignore edits that cannot change an outcome: a comment or a reformat
    must not, by itself, make a behaviour-preserving change claim a
    red-phase flip. Unparseable sources fall back to their text, which
    simply counts as changed.
    """
    signatures = {}
    for rel, text in sources.items():
        try:
            signatures[rel] = ast.dump(ast.parse(text))
        except SyntaxError:  # pragma: no cover - current tree already parsed by check_syntax
            signatures[rel] = text
    return signatures


def run_node_gate(
    node: Node,
    workdir: Path,
    *,
    baseline: str = "HEAD",
    recorder: SpanRecorder | None = None,
    capture: list[CapturedRun] | None = None,
    planned_requirements: tuple[str, ...] = (),
    owed_tests: tuple[str, ...] = (),
    tier2: bool = True,
) -> Tier1Result:
    """Run every check over `workdir` under `node`'s declaration; `baseline` is the pre-change ref.

    `planned_requirements` widens the set of requirement ids a test may
    cite without being flagged as undeclared; `owed_tests` names further
    changes still expected to add tests, which defers an uncovered changed
    line instead of failing on it. The audit passes neither, so every
    citation must be declared and every uncovered line counts.

    `tier2=False` (the checkpoint tier, `halter.auditor`) skips the three
    evidence legs only tier 2 reads -- the mutation run, the property
    oracle and all but one red-phase baseline sample (`check_red_phase`
    cannot run on none) -- so the `mutation`, `property-coverage` and
    `red-phase` checks in the result are computed over a placeholder and
    must not be read. The default is the full battery, unchanged.

    The current-tree suite runs once under coverage and its exit code
    serves both the tests check and the red-phase post leg; the baseline
    runs must be nonzero (fail or error -- a missing new test errors).
    Requirement citations are read suite-wide: every discovered test
    source counts. When `capture` is given, the suite and ruff invocations
    (exits plus output) append to it in run order.
    """
    gate = node.deterministic_gate
    sources = read_sources(workdir, "*.py")
    changed = changed_statements(workdir, git_diff(workdir, baseline, recorder=recorder))
    changed_files = sorted({path for path, _ in changed})
    added = git_added_files(workdir, baseline, recorder=recorder)
    # Every file the diff names (git decides, so deletions and non-Python
    # files count, and a staged new file is already among them -- tracked-
    # ness comes from the index), for the target-scope check.
    touched = sorted(git_changed_files(workdir, baseline, recorder=recorder))
    data_file = str(workdir / ".coverage.tier1")
    drop_test_caches(workdir)
    suite = run_shell_capture(
        under_coverage(gate.test_command, data_file), workdir, recorder=recorder
    )
    if capture is not None:
        capture.append(suite)
    current_exit = suite.exit_code
    covered = covered_lines(data_file, changed_files)
    test_sources = read_sources(workdir, "test_*.py") | read_sources(workdir, "*_test.py")
    ruff_files = [
        Path(path).relative_to(workdir).as_posix() for path in changed_files if path.endswith(".py")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        materialize_baseline(workdir, baseline, dest, recorder=recorder)
        # The ruff baseline leg, on the untouched baseline tree before
        # red-phase writes stubs and tests into it: findings the change
        # inherited are reported, not charged to it.
        at_baseline = [rel for rel in ruff_files if (dest / rel).exists()]
        baseline_findings = (
            ruff_findings(dest, at_baseline, recorder=recorder)[1] if at_baseline else []
        )
        baseline_tests = read_sources(dest, "test_*.py") | read_sources(dest, "*_test.py")
        # Captured before the two loops below write into `dest`:
        # public-deletions asks what the baseline defined, and after those
        # loops `dest` also holds stubs of modules the change created and
        # the change's own new test files -- neither of which the baseline
        # had.
        baseline_modules = {
            rel: text
            for rel, text in read_sources(dest, "*.py").items()
            if rel not in baseline_tests
        }
        tests_changed = _test_signatures(baseline_tests) != _test_signatures(test_sources)
        # Red-phase means the change's own tests against pre-change sources.
        # Without this copy the probe would run a suite that never contained
        # the new tests, and "file not found" would score as red for every
        # new file. Modules the change creates do not exist at baseline, so
        # the pre-change run cannot import them and red-phase would fall
        # back to accepting a collection error, whatever the test asserts.
        # A signature-preserving stub gives the run something to import, so
        # a test that exercises the new code fails for a real reason and a
        # tautological one passes pre-change and is rejected.
        for rel, source in sources.items():
            if rel in test_sources or (dest / rel).exists():
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_stub_module(source))
        for rel, source in test_sources.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        # Sampled, not observed once: red-phase is the only check that
        # reasons over two runs, so a flaky pre-change leg yields "fail
        # before, pass after" with no causal relation to the diff. Caches
        # are dropped between samples so each run stands alone. Skipped
        # entirely when no test changed -- that path never reads the
        # exits, and three extra suite runs is real wall-clock for
        # evidence nothing consumes. A `test` change has no baseline leg:
        # its red-phase mirrors the tests verdict, so the samples would be
        # evidence nothing reads.
        samples = (
            0 if node.kind == "test" else (RED_PHASE_SAMPLES if tests_changed and tier2 else 1)
        )
        baseline_exits: list[int] = []
        baseline_output = ""
        for sample_index in range(samples):
            drop_test_caches(dest)
            baseline_run = run_shell_capture(
                under_coverage(gate.test_command, str(dest / ".coverage.red")),
                dest,
                recorder=recorder,
            )
            baseline_exits.append(baseline_run.exit_code)
            if sample_index == 0:
                baseline_output = baseline_run.stdout + baseline_run.stderr

    if ruff_files:
        lint_run, current_findings = ruff_findings(workdir, ruff_files, recorder=recorder)
        format_run = run_capture(
            ruff_argv("format", "--check", *ruff_files), workdir, recorder=recorder
        )
        if capture is not None:
            capture.extend((lint_run, format_run))
        lint_exit, format_exit = lint_run.exit_code, format_run.exit_code
    else:
        current_findings, lint_exit, format_exit = [], 0, 0
    introduced, inherited = introduced_findings(current_findings, baseline_findings)

    sample = gate.mutation_sample
    # A `test` change alters no source, so there is nothing to mutate and
    # the check is substituted with "not required": skip the mutmut run.
    # The engine runs the declared pytest scope, the same tests the tests
    # check ran above, so a kill is scored by the tests the change is
    # judged by and by nothing outside them.
    mutation = (
        MutationOutcome(killed=0, total=0, generated=0, survivors=())
        if node.kind == "test"
        else MutationOutcome(killed=0, total=0, generated=0, survivors=(NOT_MEASURED_AT_TIER1,))
        if not tier2
        else mutation_sample(
            workdir,
            changed,
            sample.max_mutants,
            test_files=test_sources,
            run_tests=pytest_scope(gate.test_command),
            suite_passed=current_exit == 0,
            recorder=recorder,
        )
    )
    # The property oracle, `impl` changes only: the property-bearing test
    # modules that import a changed module run alone against the same
    # changed-line mutants, with the same exclusion set; `run_tests` narrows
    # what pytest collects, which `test_files` never did. `None` when no
    # module qualifies, so the check can tell "no targets" from "not run".
    # Scoped to the declared pytest scope, exactly as the mutation check
    # above is: a property module outside that scope may be red for reasons
    # unrelated to this change, and mutmut cannot baseline against a red
    # selection.
    property_candidates = tuple(property_modules(test_sources, changed_files))
    property_targets = scoped_targets(property_candidates, pytest_scope(gate.test_command))
    property_out_of_scope = tuple(p for p in property_candidates if p not in property_targets)
    property_oracle = (
        mutation_sample(
            workdir,
            changed,
            sample.max_mutants,
            test_files=test_sources,
            run_tests=property_targets,
            suite_passed=current_exit == 0,
            recorder=recorder,
        )
        if tier2 and node.kind == "impl" and property_targets
        else None
    )
    added_lines: dict[str, list[int]] = {}
    for absolute, line in changed:
        rel = Path(absolute).relative_to(workdir).as_posix()
        if rel in test_sources:
            continue
        added_lines.setdefault(rel, []).append(line)

    def suite_without(edited: Mapping[str, str]) -> int:
        """The declared test command over the tree with `edited`'s files replaced.

        Runs in a copy, so the check that asks the question cannot answer
        it by changing the tree every later check measures.
        """
        with tempfile.TemporaryDirectory(prefix="halter-dead-code-") as tmp:
            sandbox = Path(tmp) / "tree"
            shutil.copytree(workdir, sandbox, ignore=shutil.ignore_patterns("__pycache__", ".git"))
            for rel, text in edited.items():
                (sandbox / rel).write_text(text)
            return run_shell_capture(gate.test_command, sandbox, recorder=recorder).exit_code

    inputs = Tier1Inputs(
        sources=sources,
        ruff_files=ruff_files,
        ruff_introduced=tuple(introduced),
        ruff_inherited=inherited,
        ruff_lint_exit=lint_exit,
        ruff_format_exit=format_exit,
        test_runner=lambda _command: current_exit,
        changed=changed,
        covered=covered,
        baseline_exits=tuple(baseline_exits),
        baseline_output=baseline_output,
        baseline_tests=baseline_tests,
        tests_changed=tests_changed,
        current_runner=lambda: current_exit,
        flipped_tests=test_sources,
        mutation=mutation,
        added_lines={rel: tuple(sorted(lines)) for rel, lines in sorted(added_lines.items())},
        dead_code_runner=suite_without,
        baseline_sources=baseline_modules,
        workdir=str(workdir),
        owed_tests=owed_tests,
        added_files=[str(workdir / p) for p in added],
        touched_files=touched,
        test_output=suite.stdout + suite.stderr,
        workdir_modules=sorted({PurePath(rel).parts[0].removesuffix(".py") for rel in sources}),
        planned_requirements=planned_requirements,
        property_oracle=property_oracle,
        property_targets=property_targets,
        property_out_of_scope=property_out_of_scope,
    )
    return run_tier1(node, inputs)
