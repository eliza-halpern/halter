"""Evidence collectors: the subprocess wrappers and parsers the checks read.

Thin wrappers over `git`, `ruff`, `coverage` and `mutmut`, plus pure parsers
over their output, that turn a working directory into the `Tier1Inputs`
bundle `gates.run_tier1` judges. Subprocess policy lives here: the test
command's wall-clock bound, the mutation run's bound, and the memory ceiling
every process that executes the audited tree's code runs under.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePath
from time import perf_counter
from typing import Any, Final

import coverage

from halter.gates import SHELL_TIMEOUT, TOOL_UNAVAILABLE, RuffFinding
from halter.journal import SpanRecorder

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MUTANT_VERDICT = re.compile(r"^\s*(\S+): (.+?)\s*$")
"""One `mutmut results` line: the mutant's name, then any status to end of
line. mutmut 3.8's `status_by_exit_code` emits ten distinct strings (killed,
survived, no tests, check was interrupted by user, not checked, skipped,
suspicious, timeout, caught by type check, segfault), and every one of them
enters the population except `not checked`. Matching only a fixed few would
silently drop the rest -- a "no tests" mutant would vanish from both `total`
and `killed` instead of counting as a survivor."""
_MUTANT_NAME = re.compile(
    r"^(?:\w+\.)+x(?:_(?P<func>.+?)|ǁ(?P<cls>\w+)ǁ(?P<method>.+?))__mutmut_\d+$"
)
"""mutmut's two mangled-name shapes, parsed locally so halter never imports
mutmut into its own process: `<dotted.module>.x_<func>__mutmut_<n>` for a
function, `<dotted.module>.xǁ<Class>ǁ<method>__mutmut_<n>` for a method
(ǁ is U+01C1). A name that matches neither shape takes `_mutant_lines`'s
whole-file fallback."""
# Wall-clock ceiling for `mutmut run`; a run that exceeds it keeps every
# mutant decided so far.
_MUTATION_TIMEOUT_S = 600

DEFAULT_TEST_TIMEOUT_S: Final = 300.0
"""Wall-clock ceiling for the test command. A suite that exceeds it is
reported as a hang, not as a failure."""

TEST_MEMORY_LIMIT_BYTES: Final = 6 * 1024**3
"""Address-space ceiling (`RLIMIT_AS`, applied with `prlimit`) for every
subprocess that executes the audited tree's code: the test command and
`mutmut run`. Code that grows past it gets `MemoryError` in its own process
and its tests fail; the audit itself keeps running. `git`, `ruff` and
`coverage` bookkeeping calls are not capped."""


def _record(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    proc: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
    *,
    name: str | None = None,
) -> None:
    """Journal one completed invocation; no recorder means no span."""
    if recorder is None:
        return
    err = proc.stderr
    detail = err.decode(errors="replace") if isinstance(err, bytes) else err
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=proc.returncode,
        detail=detail,
        name=name,
    )


def _partial(stream: str | bytes | None) -> str:
    """Decode whatever a killed process managed to emit before dying."""
    if stream is None:
        return ""
    return stream.decode(errors="replace") if isinstance(stream, bytes) else stream


def _record_timeout(
    recorder: SpanRecorder | None,
    argv: Sequence[str],
    start: float,
    expired: subprocess.TimeoutExpired,
) -> None:
    """Journal a killed invocation; the span must not silently vanish."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=SHELL_TIMEOUT,
        detail=f"timed out after {expired.timeout}s: {_partial(expired.stderr)}",
    )


def _record_unavailable(
    recorder: SpanRecorder | None, argv: Sequence[str], start: float, exc: OSError
) -> None:
    """Journal a tool that never launched; the reason must reach the run."""
    if recorder is None:
        return
    recorder.record(
        argv=list(argv),
        duration_ms=int((perf_counter() - start) * 1000),
        exit_code=TOOL_UNAVAILABLE,
        detail=str(exc),
    )


# The lint rules the ruff check enforces: defects, not style. Every ruff
# call runs `--isolated`, so neither the audited repository's config nor
# the machine's reaches the check and a ruff upgrade's new defaults cannot
# change a verdict; `ruff check` selects exactly these: pyflakes (F), the
# E4/E7/E9 subset ruff itself ships by default, and bugbear (B).
RUFF_RULES: Final = ("F", "E4", "E7", "E9", "B")


def ruff_argv(command: str, *args: str) -> list[str]:
    """`ruff <command>` as the check runs it: isolated, and for `check`, `RUFF_RULES`."""
    argv = ["ruff", command, "--isolated"]
    if command == "check":
        argv.extend(["--select", ",".join(RUFF_RULES)])
    argv.extend(args)
    return argv


def ruff_findings(
    workdir: Path, files: Sequence[str], *, recorder: SpanRecorder | None = None
) -> tuple[CapturedRun, list[RuffFinding]]:
    """Run `ruff check --output-format json` on `files` in `workdir`.

    Returns the run (exit code as ruff gave it; stdout replaced by one
    human line per finding, `path:row:col: CODE message`) and the parsed
    findings, each carrying the stripped source line it sits on.
    Unparseable output yields no findings and leaves the exit code to say
    the tool failed.
    """
    argv = ruff_argv("check", "--output-format", "json", *files)
    run = run_capture(argv, workdir, recorder=recorder)
    findings: list[RuffFinding] = []
    try:
        raw = json.loads(run.stdout) if run.stdout.strip() else []
    except ValueError:
        raw = []
    root = os.path.realpath(workdir)
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        raw_location = item.get("location")
        location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
        row = int(location.get("row", 0) or 0)
        path = os.path.relpath(os.path.realpath(str(item.get("filename", ""))), root)
        line = _source_line(workdir / path, row)
        findings.append(
            RuffFinding(
                code=str(item.get("code") or "?"),
                path=path,
                row=row,
                message=str(item.get("message") or ""),
                line=line,
                column=int(location.get("column", 0) or 0),
            )
        )
    rendered = "\n".join(f"{f.path}:{f.row}:{f.column}: {f.code} {f.message}" for f in findings)
    shown = CapturedRun(
        argv=("ruff", "check", *files),
        exit_code=run.exit_code,
        stdout=rendered,
        stderr=run.stderr,
        timed_out=run.timed_out,
    )
    return shown, findings


def _source_line(path: Path, row: int) -> str:
    """The stripped text of line `row` (1-based) of `path`, or empty."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[row - 1].strip() if 0 < row <= len(lines) else ""


@dataclass(frozen=True)
class CapturedRun:
    """One completed invocation: argv, exit code, captured output, and whether it timed out."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_capture(
    argv: Sequence[str],
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = None,
    memory_limit: int | None = None,
) -> CapturedRun:
    """Run `argv` in `cwd`; journal its span and return exit plus output.

    On timeout the partial output is preserved and `timed_out` is set, so
    the detail still shows how far the run got before it stalled. A tool
    that cannot be launched at all yields `TOOL_UNAVAILABLE` with the OS
    error as stderr.

    `memory_limit` caps the child's address space through a `prlimit` prefix,
    not `preexec_fn`, which is unsafe once threads exist. The span and the
    result record `argv` without the prefix, so journals read as the command
    that was asked for.
    """
    start = perf_counter()
    launched = argv if memory_limit is None else ["prlimit", f"--as={memory_limit}", "--", *argv]
    try:
        proc = subprocess.run(launched, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        _record_timeout(recorder, argv, start, expired)
        return CapturedRun(
            argv=tuple(argv),
            exit_code=SHELL_TIMEOUT,
            stdout=_partial(expired.stdout),
            stderr=_partial(expired.stderr),
            timed_out=True,
        )
    except OSError as exc:
        _record_unavailable(recorder, argv, start, exc)
        return CapturedRun(argv=tuple(argv), exit_code=TOOL_UNAVAILABLE, stdout="", stderr=str(exc))
    _record(recorder, argv, start, proc)
    return CapturedRun(
        argv=tuple(argv), exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def run_shell_capture(
    command: str,
    cwd: Path,
    *,
    recorder: SpanRecorder | None = None,
    timeout: float | None = DEFAULT_TEST_TIMEOUT_S,
) -> CapturedRun:
    """Run a test-command string via shlex splitting, capturing output, under
    the `TEST_MEMORY_LIMIT_BYTES` ceiling: it executes the audited tree's code."""
    return run_capture(
        shlex.split(command),
        cwd,
        recorder=recorder,
        timeout=timeout,
        memory_limit=TEST_MEMORY_LIMIT_BYTES,
    )


def drop_test_caches(root: Path) -> None:
    """Remove Python and pytest caches so every run evaluates current sources.

    Red-phase rewrites test files into a copy seconds apart; an unchanged
    size within the same mtime second would otherwise validate a stale
    .pyc and run the previous contents. Targets materialize before removal
    so the tree never mutates mid-walk.
    """
    for cache in list(root.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for cache in list(root.rglob(".pytest_cache")):
        shutil.rmtree(cache, ignore_errors=True)
    for stale in list(root.rglob("*.pyc")):
        # TOCTOU-only: rglob yields existing paths, so the flag never fires in tests.
        stale.unlink(missing_ok=True)  # pragma: no mutate


def changed_lines(diff: str) -> set[tuple[str, int]]:
    """Added-line identities from `git diff -U0` text; deletions add none."""
    changed: set[tuple[str, int]] = set()
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line.removeprefix("+++ ").strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif path is not None and (match := _HUNK.match(line)):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            changed.update((path, number) for number in range(start, start + count))
    return changed


def git_changed_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Paths, relative to `cwd`, that differ from `ref`: what the change touched.

    `--no-renames`: with git's default rename detection a staged
    `git mv n.py m.py` prints only `m.py`, and the path the change deleted
    would never reach the target-scope check. Both paths are the change's.
    """
    argv = ["git", "-C", str(cwd), "diff", "--no-renames", "--name-only", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --name-only against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


def git_added_files(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> list[str]:
    """Paths, relative to `cwd`, that do not exist at `ref` (renames count as added).

    Only staged adds count: `audit_tree` stages the whole tree before the
    checks run, so every file the change creates is staged, while the
    files the checks themselves leave behind (`.coverage.tier1`, bytecode)
    stay untracked and are not listed.
    """
    argv = [
        "git",
        "-C",
        str(cwd),
        "diff",
        "--no-renames",
        "--diff-filter=A",
        "--name-only",
        ref,
        "--",
        ".",
    ]
    start = perf_counter()
    proc = subprocess.run(argv, capture_output=True, text=True)
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff --diff-filter=A against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.splitlines()


def git_diff(cwd: Path, ref: str, *, recorder: SpanRecorder | None = None) -> str:
    """Zero-context diff of the tree at `cwd` against git `ref`."""
    argv = ["git", "-C", str(cwd), "diff", "-U0", ref, "--", "."]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
    )
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git diff against {ref!r} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout


def materialize_baseline(
    cwd: Path, ref: str, dest: Path, *, recorder: SpanRecorder | None = None
) -> None:
    """Extract tracked files at git `ref` into `dest` (empty when the tree is empty)."""
    argv = ["git", "-C", str(cwd), "archive", ref]
    start = perf_counter()
    proc = subprocess.run(
        argv,
        capture_output=True,
    )
    _record(recorder, argv, start, proc)
    if proc.returncode != 0:
        msg = f"git archive of {ref!r} failed: {proc.stderr.decode().strip()}"
        raise RuntimeError(msg)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
            tar.extractall(dest, filter="data")
    except tarfile.ReadError:
        # `git archive` of an empty tree is not a readable tar: nothing to extract.
        return


def under_coverage(test_command: str, data_file: str) -> str:
    """Rewrite a `pytest` command so the run records coverage into `data_file`."""
    return test_command.replace("pytest", f"coverage run --data-file={data_file} -m pytest", 1)


def pytest_scope(test_command: str) -> tuple[str, ...]:
    """The arguments a test command hands to pytest, in order.

    Everything after the first `pytest` token, so the mutation run
    collects the same tests the tests check ran. A command that never
    names pytest yields nothing, and the mutation run collects its whole
    tree.
    """
    argv = shlex.split(test_command)
    if "pytest" not in argv:
        return ()
    return tuple(argv[argv.index("pytest") + 1 :])


def scoped_targets(targets: Collection[str], scope: Collection[str]) -> tuple[str, ...]:
    """`targets` the pytest `scope` would itself collect.

    The property oracle runs its targets ALONE against the change's
    mutants, and mutmut baselines by running that selection, so one
    module the scope excludes -- which may be red for reasons unrelated
    to this change -- would decide the whole oracle.

    An empty `scope` is pytest's whole tree, so nothing is filtered.
    Flags are not selectors and are ignored; a `path::name` selector
    scopes by its path. What this admits, stated plainly: a change whose
    only property-bearing module lies outside its scope gets no oracle
    at all, the same way the mutation check already runs only the tests
    in scope.
    """
    roots = tuple(
        PurePath(item.split("::", 1)[0]).as_posix().rstrip("/")
        for item in scope
        if not item.startswith("-")
    )
    if not roots:
        return tuple(targets)
    kept = []
    for target in targets:
        path = PurePath(target).as_posix()
        if any(path == root or path.startswith(root + "/") for root in roots):
            kept.append(target)
    return tuple(kept)


type MutantDetail = tuple[str, str, str]  # (name, status, mutmut show text)


@dataclass(frozen=True)
class MutationOutcome:
    """Sampled kill-rate evidence over changed-line mutants."""

    killed: int
    total: int
    generated: int
    survivors: tuple[str, ...]
    # Surviving mutants whose only change is inside string literals: a
    # test cannot kill one without pinning wording, so they leave the
    # population and are counted here instead.
    text_only: int = 0
    # Where each survivor sits: the changed lines its removed hunk lines
    # matched, spelled as the caller spelled `changed`, so a reader can
    # name the enclosing function without re-running mutmut.
    survivor_lines: tuple[tuple[str, int], ...] = ()
    # How many decided mutants scored "no tests": no test executes the
    # mutated function at all (mutmut's own exit codes 33 and 5). Their
    # names are already in `survivors` and their lines in `survivor_lines`;
    # this is only the count for the check's detail string.
    untested: int = 0
    # Every scored mutant's status string, counted after the text-only
    # exclusion and the `not checked` drop, so the counts always sum to
    # `total`. Nothing in `gates` reads this; it is carried into the JSON
    # result so a reader can see the raw status distribution (mutmut 3.8
    # maps SIGKILL and SIGSEGV both to "segfault", and can escalate a
    # timeout to it). A mutmut failure (`total == 0`) leaves it empty.
    statuses: tuple[tuple[str, int], ...] = ()
    mutant_detail: tuple[MutantDetail, ...] = field(default=(), compare=False)
    """(name, status, mutmut show text) for EVERY scored mutant, killed ones
    included, in name order; a mutant mutmut never scored (`not checked`) or
    that is not in `total` has none. Recording only: no verdict reads it, and
    neither `--json` report prints it (the tiered verdict cache keeps it)."""


def _is_given(decorator: ast.expr) -> bool:
    """True when `decorator` is `given` or `given(...)`, bare or attribute-qualified."""
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )
    return name == "given"


def _imported_names(tree: ast.AST) -> set[str]:
    """Dotted names a module imports: `a.b` and, for `from a import b`, `a` and `a.b`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


def property_modules(
    test_sources: Mapping[str, str], changed_files: Collection[str]
) -> dict[str, str]:
    """Test modules that drive a `@given` property over a changed module.

    A module counts when its AST carries a `given` decorator and one of
    its imports names a changed file: the dotted name, as a path, equals
    the changed file's path without `.py` (a package's `__init__.py` is
    its directory) or ends it at a `/` boundary. These are the modules
    the property oracle runs alone against the change's mutants.
    """
    targets: set[str] = set()
    for path in changed_files:
        posix = PurePath(path).as_posix().removesuffix(".py")
        targets.add(posix.removesuffix("/__init__") if posix.endswith("/__init__") else posix)
    matched: dict[str, str] = {}
    for path in sorted(test_sources):
        source = test_sources[path]
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        functions = (
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        if not any(_is_given(d) for f in functions for d in f.decorator_list):
            continue
        for name in _imported_names(tree):
            as_path = name.replace(".", "/")
            if any(t == as_path or t.endswith("/" + as_path) for t in targets):
                matched[path] = source
                break
    return matched


def _parse_mutant_verdicts(text: str) -> dict[str, str]:
    """Mutant name to verdict from `mutmut results --all True` output.

    Every decided status, not only killed/survived/timeout/not-checked:
    a status this parser cannot match is a status that silently leaves
    both `total` and `killed` in `mutation_sample`.
    """
    verdicts = {}
    for line in text.splitlines():
        match = _MUTANT_VERDICT.match(line)
        if match:
            verdicts[match.group(1)] = match.group(2)
    return verdicts


def _mutant_path(show_output: str) -> str | None:
    """Repo-relative path from `mutmut show`, else None."""
    for line in show_output.splitlines():
        if line.startswith("+++ "):
            return line[4:].strip().removeprefix("b/")
    return None


_FSTRING_TEXT: Final = frozenset(
    getattr(tokenize, n) for n in ("FSTRING_MIDDLE",) if hasattr(tokenize, n)
)


def _tokens_modulo_strings(line: str) -> list[tuple[int, str]] | None:
    """A line's tokens with every string literal's text blanked, or None when
    the line does not tokenize on its own (a multi-line construct)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line + "\n").readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    out: list[tuple[int, str]] = []
    for tok in tokens:
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.COMMENT):
            continue
        if tok.type in _FSTRING_TEXT:
            # An f-string's literal text arrives as a variable number of
            # middle tokens (3.12+); it is string content, so it is dropped
            # rather than blanked, and the expressions between stay.
            continue
        out.append((tok.type, "" if tok.type == tokenize.STRING else tok.string))
    return out


def text_only_mutant(show_output: str) -> bool:
    """Whether a `mutmut show` diff changes nothing but string-literal text.

    A mutant that only edits a message (`"cannot convert"` to
    `"XXcannot convertXX"`) can be killed only by a test that pins the
    wording. Pairwise: the removed and added lines must tokenize
    identically once string contents are blanked, and at least one string
    must differ. Anything that does not tokenize line by line stays in
    the population (fail closed).

    This is a shape test, not a semantic one, and it cannot be more:
    whether a string's contents matter is a property of the program, not
    of the code's shape -- a string can equally be a currency code or a
    `Decimal` exponent, and a mutant of those is a behaviour change. So
    `mutation_sample` consults this only for SURVIVORS, where an exclusion
    costs nothing it could have learned; a killed mutant of any shape is
    evidence the suite discriminates and stays in the population.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    added = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    ]
    if not removed or len(removed) != len(added):
        return False
    differs = False
    for old, new in zip(removed, added, strict=True):
        if old == new:
            continue
        a, b = _tokens_modulo_strings(old), _tokens_modulo_strings(new)
        if a is None or b is None or a != b:
            return False
        differs = True
    return differs


def _mutant_def(
    tree: ast.Module, match: re.Match[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The `def` mutmut mutated, from `_MUTANT_NAME`'s parse of `mutant_name`.

    A method resolves only inside its own top-level `ClassDef`'s direct
    children (name alone is not enough: two classes, or a module-level
    function sharing a method's name, must not collide). A function
    resolves only among top-level `def`s.
    """
    cls_name = match.group("cls")
    func_name = match.group("func") or match.group("method")
    scope: list[ast.stmt] = tree.body
    if cls_name is not None:
        class_node = next(
            (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name),
            None,
        )
        if class_node is None:
            return None
        scope = class_node.body
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    return None


def _statement_start(tree: ast.Module, line: int) -> int | None:
    """First line of the innermost `ast.stmt`/`ast.excepthandler` in `tree`
    whose `[lineno, end_lineno]` contains `line`; `None` if none does.

    This is the coordinate system `statement_lines` (and so `changed`)
    uses, built by taking the enclosing statement with the smallest span --
    a nested statement's range is always a subset of its parents', so the
    smallest one containing `line` is the innermost. Walking the whole
    module rather than just the resolved def's own subtree is deliberate:
    the def's own search range in `_mutant_lines` is what keeps a
    duplicate line elsewhere in the file out of `matched` in the first
    place, and this function does not re-derive that scoping.
    """
    best: ast.stmt | ast.excepthandler | None = None
    best_span = -1
    for child in ast.walk(tree):
        if not isinstance(child, (ast.stmt, ast.excepthandler)):
            continue
        end = child.end_lineno or child.lineno
        if not (child.lineno <= line <= end):
            continue
        span = end - child.lineno
        if best is None or span < best_span:
            best, best_span = child, span
    return best.lineno if best is not None else None


def changed_statements(workdir: Path, diff: str) -> set[tuple[str, int]]:
    """First line of every statement with at least one of its own lines changed.

    A line belongs to a statement if it is non-blank, not a comment, and
    inside the statement's span; a decorator line belongs to the `def` or
    `class` it decorates. Docstrings stay exempt (`statement_lines`), and the
    spelling is `(str(workdir / rel), line)`, the one `changed` has always
    used. Only `.py` files that exist under `workdir` and parse contribute.
    Mapping every changed line to its statement's first line is what keeps
    a change confined to a continuation line, such as the message of a
    multi-line `raise`, from passing coverage with "no changed lines".
    """
    by_path: dict[str, set[int]] = {}
    for rel, number in changed_lines(diff):
        by_path.setdefault(rel, set()).add(number)
    found: set[tuple[str, int]] = set()
    for rel, numbers in by_path.items():
        path = workdir / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        text = source.splitlines()
        decorated = [
            (decorator.lineno, decorator.end_lineno or decorator.lineno, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for decorator in node.decorator_list
        ]
        executable = statement_lines(source)
        for number in numbers:
            stripped = text[number - 1].strip()
            if not stripped or stripped.startswith("#"):
                continue
            start = next(
                (owner for first, last, owner in decorated if first <= number <= last),
                _statement_start(tree, number),
            )
            if start in executable:
                found.add((str(workdir / rel), start))
    return found


def _mutant_lines(show_output: str, source: str, mutant_name: str) -> set[int]:
    """Statement-start line numbers the mutant's removed (`-`) hunk lines locate to.

    Scoped to the function mutmut actually mutated: a method mutant on a
    changed line enters the population, a mutant on a continuation line
    of a changed statement enters it, and a mutant whose text merely
    repeats a changed line elsewhere in the file does not.

    `mutant_name`'s two shapes (`_MUTANT_NAME`) resolve a `def` with `ast`
    (`_mutant_def`). Only lines inside that def's own range -- from its
    first decorator line (or the `def` line) to `end_lineno` -- can match,
    at the def's own indentation added back (mutmut renders the extracted
    function at column 0, so every line including a continuation loses
    that one level of dedent). Restricting `matched` to the def's own
    range is the only thing standing between a duplicate line elsewhere
    in the file and a wrong attribution: `_statement_start` looks up the
    innermost statement over the *whole* module, not just this def's
    subtree, so a matched line is trusted to belong to this def only
    because the range already confined it there. A matched line the whole
    module covers by no statement at all -- a decorator, since
    `ast.FunctionDef.lineno` is the `def` line and never the decorator's
    -- falls back to the def's own line, `statement_lines`'s coordinate
    for the whole decorated function.

    A name that matches neither shape (a hand-built stub in a test) takes
    a whole-file exact match with no statement mapping.
    """
    removed = [
        line[1:]
        for line in show_output.splitlines()
        if line.startswith("-") and not line.startswith("--- ")
    ]
    if not removed:
        return set()
    match = _MUTANT_NAME.match(mutant_name)
    if match is None:
        numbered = list(enumerate(source.splitlines(), start=1))
        return {lineno for lineno, text in numbered for snippet in removed if text == snippet}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    node = _mutant_def(tree, match)
    if node is None:
        return set()
    lines = source.splitlines()
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    end = min(node.end_lineno or node.lineno, len(lines))
    indent_match = re.match(r"[ \t]*", lines[node.lineno - 1])
    indent = indent_match.group() if indent_match else ""
    matched = {
        lineno
        for lineno in range(start, end + 1)
        for snippet in removed
        if lines[lineno - 1] == indent + snippet
    }
    return {(_statement_start(tree, lineno) or node.lineno) for lineno in matched}


def _mutmut_scratch_config(sources: list[str], run_tests: Collection[str] = ()) -> str:
    """Minimal mutmut config: per-file sources (a `.` root nests mutants/).

    `run_tests` are pytest arguments appended after the fixed flags, so
    only what they collect runs against each mutant; empty means the
    whole scratch tree. A kill scored by a narrowed set belongs to that
    set, which the unrestricted run cannot say. A sequence keeps its
    order (a scope's `-k expr` must stay a pair); an unordered collection
    is sorted for a stable file.
    """
    quoted = ", ".join(json.dumps(source) for source in sources)
    ordered = list(run_tests) if isinstance(run_tests, Sequence) else sorted(run_tests)
    args = ["-q", "-x", "-p", "no:cacheprovider", *ordered]
    joined = ", ".join(json.dumps(arg) for arg in args)
    return f"[tool.mutmut]\nsource_paths = [{quoted}]\npytest_add_cli_args = [{joined}]\n"


class MutantLookupError(RuntimeError):
    """The batched mutant-lookup subprocess failed or gave unparseable output.

    A lookup failure must be named, never silently read as "no mutants",
    the same rule the mutation check applies to `mutmut run` itself.
    """


# The first line is a marker: argv for this call is `[sys.executable, "-c",
# _MUTANT_LOOKUP_SCRIPT]`, so the marker lands in the recorded span's argv
# and a journal reader can recognise the lookup. Reads every mutant's diff
# in one process: `SourceFileMutationData` and `get_diff_for_mutant` are
# the same functions `mutmut show NAME` calls (`mutmut/__main__.py:show`,
# `mutmut/mutation/diff_apply.py`), so walking `walk_mutatable_files()`
# once and loading each file's meta once reproduces `mutmut show`'s stdout
# for every mutant without one subprocess per mutant. The first file to
# name a key wins, matching `find_mutant`. A name whose diff raises keeps
# only the header line, exactly what `show` prints to stdout before its
# traceback.
_MUTANT_LOOKUP_SCRIPT: Final = r"""# halter-mutant-lookup
import json
import sys

from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.diff_apply import get_diff_for_mutant
from mutmut.stats import status_by_exit_code
from mutmut.utils.file_utils import walk_mutatable_files

mapping: dict[str, str] = {}
for path in walk_mutatable_files():
    data = SourceFileMutationData(path=path)
    data.load()
    for name, exit_code in data.exit_code_by_key.items():
        if name in mapping:
            continue
        header = f"# {name}: {status_by_exit_code[exit_code]}\n"
        try:
            diff = get_diff_for_mutant(name, path=data.path)
        except Exception:
            mapping[name] = header
        else:
            mapping[name] = header + diff + "\n"
json.dump(mapping, sys.stdout)
"""


def _lookup_failure(run: CapturedRun) -> MutantLookupError:
    """One line: the lookup subprocess's exit code and its last stderr line."""
    lines = run.stderr.strip().splitlines()
    last = lines[-1] if lines else "no output"
    msg = f"exit {run.exit_code}: {last}"
    return MutantLookupError(msg)


def show_all_mutants(scratch: Path, *, recorder: SpanRecorder | None = None) -> dict[str, str]:
    """`mutmut show NAME`'s stdout for every mutant, in one subprocess.

    Runs `_MUTANT_LOOKUP_SCRIPT` with `cwd=scratch`: mutmut's `config()` is a
    process-global cache read from `./pyproject.toml` and
    `read_mutants_module` opens `Path("mutants") / path` relative to the
    working directory, so the lookup must run as a subprocess rooted at
    `scratch` rather than inside halter's own process. Raises
    `MutantLookupError` on a non-zero exit or unparseable stdout.
    """
    run = run_capture([sys.executable, "-c", _MUTANT_LOOKUP_SCRIPT], scratch, recorder=recorder)
    if run.exit_code != 0:
        raise _lookup_failure(run)
    try:
        mapping = json.loads(run.stdout)
    except ValueError:
        raise _lookup_failure(run) from None
    if not isinstance(mapping, dict) or not all(isinstance(v, str) for v in mapping.values()):
        raise _lookup_failure(run)
    return mapping


def mutation_sample(
    workdir: Path,
    changed: Collection[tuple[str, int]],
    max_mutants: int,
    *,
    test_files: Collection[str],
    run_tests: Collection[str] = (),
    suite_passed: bool = True,
    timeout_s: int = _MUTATION_TIMEOUT_S,
    recorder: SpanRecorder | None = None,
) -> MutationOutcome:
    """Kill-rate over every decided mutant on a changed line.

    Runs in a scratch copy (mutmut writes mutants/ into cwd) under a time
    budget; the verdict covers every changed-line mutant mutmut decided
    and degrades to the decided ones when the budget binds first (a real
    timeout, not a truncation). `max_mutants` is kept as a parameter
    because the declaration schema carries it; it no longer samples or
    truncates the population, so no verdict depends on which mutants sort
    first by name. `test_files` are excluded from mutation scope (mutating
    tests pollutes the rate); `run_tests` restricts which tests pytest
    collects against each mutant and leaves the scope alone -- the two
    are different sets. Text-only mutants are excluded only when they did
    NOT kill. Timeouts count as killed (behaviour changed), and missing
    mutmut fails closed. Every decided mutant on a changed line enters
    the population: `killed` and `timeout` are the only killed statuses,
    `not checked` stays undecided, and everything else -- `no tests`
    included -- is a survivor; `no tests` ones are also counted in
    `MutationOutcome.untested`.
    """
    if not changed:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=())
    if shutil.which("mutmut") is None:
        return MutationOutcome(killed=0, total=0, generated=0, survivors=("mutmut not on PATH",))
    root = os.path.realpath(workdir)
    by_line: dict[str, set[int]] = {}
    spelled: dict[str, str] = {}
    for path, line in changed:
        key = os.path.relpath(os.path.realpath(path), root)
        by_line.setdefault(key, set()).add(line)
        spelled.setdefault(key, path)
    tests = set(test_files)
    with tempfile.TemporaryDirectory(prefix="halter-mutation-") as tmp:
        scratch = Path(tmp)
        for source in sorted(workdir.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            dest = scratch / source.relative_to(workdir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        production = sorted(
            path.relative_to(scratch).as_posix()
            for path in scratch.rglob("*.py")
            if path.relative_to(scratch).as_posix() not in tests
        )
        if not production:
            return MutationOutcome(killed=0, total=0, generated=0, survivors=())
        (scratch / "pyproject.toml").write_text(_mutmut_scratch_config(production, run_tests))
        ran = run_capture(
            ["timeout", str(timeout_s), "mutmut", "run"],
            scratch,
            recorder=recorder,
            memory_limit=TEST_MEMORY_LIMIT_BYTES,
        )
        # `mutmut run` exits 0 even when mutants survive, so any other exit
        # is the tool failing, not a verdict (mutmut 3.8 refuses a package
        # named `src`, for one), and reading it as "no mutants decided"
        # would hide the tool's failure. SHELL_TIMEOUT is the budget
        # binding and keeps its path.
        if ran.exit_code not in (0, SHELL_TIMEOUT):
            output = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
            last = output[-1].strip() if output else "no output"
            # The same exit covers two causes and only the caller can tell
            # them apart: mutmut baselines by running the suite, so a red
            # suite fails collection exactly as a broken mutmut does.
            # `suite_passed` is the tests check's own verdict on this tree;
            # when it is False the suite is the cause and mutmut is not.
            cause = f"mutmut run exited {ran.exit_code}: {last}"
            return MutationOutcome(
                killed=0,
                total=0,
                generated=0,
                survivors=(cause if suite_passed else f"suite is red: {cause}",),
            )
        results = run_capture(["mutmut", "results", "--all", "True"], scratch, recorder=recorder)
        verdicts = _parse_mutant_verdicts(results.stdout)
        try:
            shows = show_all_mutants(scratch, recorder=recorder)
        except MutantLookupError as exc:
            # A lookup failure is named, never read as "no mutants", the
            # same rule as for `mutmut run` above.
            msg = f"mutant lookup failed: {exc}"
            return MutationOutcome(killed=0, total=0, generated=0, survivors=(msg,))
        scoped: list[tuple[str, str, str, set[int]]] = []
        shown: dict[str, str] = {}
        undecided = 0
        text_only = 0
        for name in sorted(verdicts):
            verdict = verdicts[name]
            if verdict == "not checked":
                undecided += 1
                continue
            shown_stdout = shows.get(name, "")
            rel = _mutant_path(shown_stdout)
            if rel is None:
                continue
            posix_rel = rel.replace(os.sep, "/")
            if posix_rel in tests:
                continue
            key = os.path.relpath(os.path.realpath(scratch / rel), os.path.realpath(scratch))
            lines = by_line.get(key)
            if lines is None:
                continue
            target = scratch / rel
            if not target.is_file():
                continue
            hit = _mutant_lines(shown_stdout, target.read_text(), name) & lines
            if not hit:
                continue
            # Only a mutant that did NOT kill is excluded as text-only
            # (every not-killed status, not just "survived": the argument
            # that no test can kill a message-only mutant without pinning
            # wording does not depend on whether a test currently runs the
            # function). The tokenizer cannot tell a message from a
            # currency code or a `Decimal` exponent, so excluding a killed
            # text-only mutant would drop real behaviour changes the suite
            # caught from both sides of the ratio. A kill is evidence the
            # suite discriminates; the verdict decides, not the shape.
            if verdict not in ("killed", "timeout") and text_only_mutant(shown_stdout):
                text_only += 1
                continue
            scoped.append((name, verdict, key, hit))
            shown[name] = shown_stdout
    sample = scoped
    killed = sum(1 for _, verdict, _, _ in sample if verdict in ("killed", "timeout"))
    # Every not-killed status is a survivor, not only "survived": `no
    # tests`, `suspicious`, `segfault` and the rest all count against the
    # change exactly as a survived mutant does.
    survivors = tuple(
        name for name, verdict, _, _ in sample if verdict not in ("killed", "timeout")
    )
    survivor_lines = sorted(
        {
            (spelled[key], line)
            for _, verdict, key, hit in sample
            if verdict not in ("killed", "timeout")
            for line in hit
        }
    )
    untested = sum(1 for _, verdict, _, _ in sample if verdict == "no tests")
    status_tally: dict[str, int] = {}
    for _, verdict, _, _ in sample:
        status_tally[verdict] = status_tally.get(verdict, 0) + 1
    return MutationOutcome(
        killed=killed,
        total=len(sample),
        generated=len(scoped) + undecided,
        survivors=survivors,
        text_only=text_only,
        survivor_lines=tuple(survivor_lines),
        untested=untested,
        statuses=tuple(sorted(status_tally.items())),
        mutant_detail=tuple((name, verdict, shown[name]) for name, verdict, _, _ in sample),
    )


def covered_lines(data_file: str, files: Collection[str]) -> set[tuple[str, int]]:
    """Executed lines per file from a coverage data file (empty when unreadable).

    Data keys are absolute while callers may pass workdir-relative paths, so
    the join normalizes both sides; emitted tuples keep the caller's spelling.
    """
    cov = coverage.Coverage(data_file=data_file, config_file=False)
    try:
        cov.load()
    except coverage.CoverageException:
        return set()
    data = cov.get_data()
    by_realpath = {os.path.realpath(measured): measured for measured in data.measured_files()}
    covered: set[tuple[str, int]] = set()
    for filename in files:
        measured = by_realpath.get(os.path.realpath(filename))
        if measured is None:
            continue
        for number in data.lines(measured) or ():
            covered.add((filename, number))
    return covered


def _docstring_lines(tree: ast.Module) -> set[int]:
    """First lines of docstrings: leading strings of modules, classes, functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(first.lineno)
    return found


def statement_lines(source: str) -> set[int]:
    """Executable first-lines of `source`; unparseable source yields none.

    Conservative approximation of what coverage can execute (blanks,
    comments, and docstrings are never statements; `case` lines carry
    no position of their own and stay exempt). A syntax error means the
    syntax check fails the change anyway, so there is nothing coverable
    to require here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines = {
        node.lineno for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.excepthandler))
    }
    return lines - _docstring_lines(tree)
