# halter

An auditor for a change to a Python tree.

`halter` takes one git revision (or the working tree) of a Python
repository, runs the tree's own tests and a battery of checks over the
diff against a baseline commit, and answers **accept** or **refuse** with
one line per check. It has no configuration and no network: everything it
decides is decided from the tree, the tools it shells out to, and its own
check code, so two audits of the same tree with the same tools give the
same answer.

## What it audits

For the diff `baseline..tree`, in this order:

| check                    | what passes                                                                 |
|--------------------------|-----------------------------------------------------------------------------|
| `syntax`                 | every changed Python file parses                                            |
| `ruff`                   | the diff introduces no new `ruff check` finding and is `ruff format`-clean  |
| `tests`                  | the test command exits 0                                                    |
| `coverage`               | every changed line runs under the tests (changed-line coverage 100%)        |
| `dead-code`              | every private definition the diff adds is mentioned somewhere in the tree   |
| `public-deletions`       | every public definition the baseline had is still defined                   |
| `red-phase`              | new tests fail before the change and pass after it; with no test change, coverage plus a mutation kill-rate of at least 85% carries the proof |
| `assertion-preservation` | pre-existing tests keep their assertions                                    |
| `mutation`               | `mutmut` mutants on the changed lines are killed by the tests (100% on small samples) |

Four checks judge a change against a declaration about it (which kind of
change it is, which files it may touch, which requirements it binds, which
property test covers it). A bare diff declares nothing, so `node-scope`,
`target-scope`, `property-coverage` and `requirement-binding` are reported
`n/a` rather than `PASS`. Untracked files in the working tree count as
part of the change;
`mutants/`, `.coverage`, bytecode and tool caches at the tree's top level
do not.

Verdicts are cached under `~/.cache/halter/audit` keyed by the tree hash,
the baseline, the test command and a hash of halter's own check modules
and tool versions, so a second audit of the same tree is served without
re-running anything. `--no-cache` disables that.

## Tiered mode

`halter --tiered` runs the same checks split into three tiers and reports
one finding per check instead of one line per check. It is opt-in: without
the flag, halter's report, JSON and verdict are exactly as before.

| tier | checks | runs |
|------|--------|------|
| 0 | `syntax`, `ruff`, `imports` | once per changed Python file, on that file's text alone; no tests run |
| 1 | `tests`, `coverage`, `dead-code`, `public-deletions`, `node-scope`, `target-scope`, `assertion-preservation` | the test command once under coverage, one red-phase baseline sample, no mutation run |
| 2 | `mutation`, `property-coverage`, `red-phase`, `requirement-binding`, `full-suite` | the full battery; `full-suite` is the `tests` check of that run |

- `imports` is the one check the default mode does not have: every
  absolute import's top-level name must be the standard library, a module
  in the tree (its root, the file's directory or `src/`), or importable by
  **halter's own interpreter**. A tree whose dependencies are installed
  only in another environment fails it; install them where halter runs
  (the mutation run already needs them there).
- Tier 2 never runs on a tree whose tier 1 failed. It reports a single
  finding, `blocked`, naming the tier-1 checks that failed, and the verdict
  is `refuse`. Mutation, red-phase and the other tier-2 checks are then
  neither run nor reported.
- Each finding carries a reason: `code-wrong` (syntax, ruff, imports,
  tests, public-deletions, full-suite), `evidence-thin` (coverage,
  dead-code, assertion-preservation, mutation, property-coverage,
  red-phase, requirement-binding), `scope` (node-scope, target-scope) or
  `unknown` (a `blocked` tier 2, or a mutation tool that decided nothing).
- Finding verdicts are `pass`, `fail`, `not-applicable` or `blocked`
  (`not-proven` too under `--tier2 shortlist`, below); the run's verdict is
  `accept` only when every tier passed. Exit codes are
  the same four as the default mode; an unchanged tree prints the default
  mode's `nothing to audit` report and exits 3.
- Tier 1 and tier 2 each run the test command, so a tiered audit runs the
  tests at least twice where the default mode runs them once.
- Verdicts are cached per tier in the same `--cache` directory, keyed by
  the staged tree (tier 0: the file's path and bytes), the baseline, the
  test command and the check surface.

With `--json` the output is `{"verdict": ..., "tiers": [{"tier", "key",
"passed", "cached", "findings": [{"gate", "tier", "verdict", "reason",
"detail", "cites"}]}]}`; `cites` names the function the verdict came from,
then the check's basis when it has one.

### Tier 2 as a survivor shortlist

`halter --tier2 shortlist` (it implies `--tiered`) changes what tier 2's
`mutation` finding decides on. The default, `--tier2 score`, is the
kill-rate bar and is unchanged.

- The kill rate is still computed and shown in `cites`, marked "recorded,
  not decisive". What the finding reports is each surviving changed-line
  mutant, `no tests` ones included, one per line first. The detail names
  up to `--mutant-shortlist N` of them (default 5) with the file, line, the
  line's text and the mutation, then "(and K more)".
- A survivor whose only change is inside the argument of a raised
  exception or a logging call (decided on the AST) is excluded and
  counted. One whose change is only message or field-name text, or the one
  known equivalent pattern (`Decimal(1)` to `Decimal(2)` as a quantize
  exponent), is set aside and named with the rule. Survivors in untested
  code or that change behaviour are never set aside.
- An open survivor makes the finding `not-proven`, and a changed line no
  test runs makes tier 1's `coverage` finding `not-proven` rather than
  `fail`, so tier 2 still runs. **`not-proven` does not refuse**: it is
  reported, and the run can still `accept`. Every other check refuses as
  in the default mode.
- With `--json`, tier 2 also carries `survivors`: every open survivor as
  `{path, line, name, status, mutation, source, behaviour}`, where
  `behaviour` is the enclosing function's name and the first line of its
  docstring.
- The shortlist mode and `N` are part of the cache key.

## Exit codes

| exit | verdict            | meaning                                                               |
|------|--------------------|-----------------------------------------------------------------------|
| 0    | `accept`           | every applicable check passed                                         |
| 1    | `refuse`           | at least one check failed; the report says which                      |
| 2    | could not audit    | not a git repository, unknown revision or baseline, a linked worktree |
| 3    | `nothing to audit` | tree and baseline are identical                                       |

With `--json` the result dict is printed instead of the text report; the
exit code is the same.

## Usage

    halter                       # the working tree of . against HEAD
    halter REV                   # commit REV against REV^, from a fresh clone
    halter REV --baseline B      # commit REV against B
    halter --repo PATH           # audit another repository
    halter --test-command CMD    # default: python -m pytest -q
    halter --json                # machine-readable result
    halter --no-cache            # neither read nor write the verdict cache
    halter --tiered              # tiers 0-2, one finding per check
    halter --tier2 shortlist     # tiered; tier 2 names surviving mutants

In `REV` mode the commit is checked out in a temporary clone, so nothing
uncommitted in the source repository reaches the checks and nothing is
written to it.

## Prerequisites

- Python >= 3.12.
- From pip, installed with halter: `coverage`, `ruff`, `mutmut` (3.x) and
  `pydantic`. halter calls `ruff`, `coverage` and `mutmut` **by name**, so
  the environment that provides `halter` must also put them on `PATH`
  (`pipx install halter` and a plain venv both do).
- From the OS: `git`, and `prlimit` (util-linux) for the memory ceiling.
- The audited tree's own test command must be runnable from the tree with
  the interpreter on `PATH`; the default is `python -m pytest -q`, so the
  tree's dependencies and `pytest` must be importable there.

## Memory ceiling and timeouts

Every subprocess that executes the audited tree's code, that is the test
command and `mutmut run`, is launched under `prlimit --as=6GiB`
(`RLIMIT_AS`, 6 GiB). Code that grows past it gets a `MemoryError` in its
own process and its tests fail; the audit itself keeps running.

The test command is bounded at 300 s and the mutation run at 600 s of
wall clock. A run that exceeds its bound is reported as a failed check,
not a crash.

## A worked example

A throwaway repository with two commits: `n.py` returning 1, then a fix
to 2 with a test.

    git init example && cd example
    printf 'def f():\n    return 1\n' > n.py
    git add -A && git commit -m baseline
    printf 'def f():\n    return 2\n' > n.py
    printf 'from n import f\n\n\ndef test_f():\n    assert f() == 2\n' > test_n.py
    git add -A && git commit -m 'fix f'

Audit the second commit:

    $ halter HEAD
    audit tree d6ec5987d44d baseline b32d48a01db2 surface 374ed4f61324 fresh
    PASS syntax                  2 file(s) parsed
    PASS ruff                    2 file(s) clean
    PASS tests                   'python -m pytest -q' exited 0
    PASS coverage                every changed line runs
    PASS dead-code               every private definition added is mentioned elsewhere in the tree
    PASS public-deletions        every public definition the baseline had is still defined
    PASS red-phase               fail pre-change, pass post-change
    n/a  node-scope              not applicable: halter audits a bare diff, no change kind is declared
    n/a  target-scope            not applicable: halter audits a bare diff, no target files are declared
    n/a  property-coverage       not applicable: halter audits a bare diff, no property test is declared
    PASS assertion-preservation  0 pre-existing test(s) keep their assertions
    n/a  requirement-binding     not applicable: halter audits a bare diff, no requirement is declared
    PASS mutation                killed 1 of 1 changed-line mutants (100.0% >= 100.0%)
    verdict: accept
    $ echo $?
    0

Now add an untested module to the working tree and audit that:

    $ printf 'def g():\n    return 7\n' > m.py
    $ halter
    audit tree cf04ccbe17e6 baseline 381ea238b0de surface 374ed4f61324 fresh
    PASS syntax                  3 file(s) parsed
    PASS ruff                    1 file(s) clean
    PASS tests                   'python -m pytest -q' exited 0
    FAIL coverage                no test runs m.py:1, m.py:2
    PASS dead-code               every private definition added is mentioned elsewhere in the tree
    PASS public-deletions        every public definition the baseline had is still defined
    FAIL red-phase               tests unchanged and coverage failed; nothing proves the change
    n/a  node-scope              not applicable: halter audits a bare diff, no change kind is declared
    n/a  target-scope            not applicable: halter audits a bare diff, no target files are declared
    n/a  property-coverage       not applicable: halter audits a bare diff, no property test is declared
    PASS assertion-preservation  1 pre-existing test(s) keep their assertions
    n/a  requirement-binding     not applicable: halter audits a bare diff, no requirement is declared
    FAIL mutation                killed 0 of 1 changed-line mutants (0.0% < 100.0%) (small sample: 1 mutant(s), all must die): survived 1: m.x_g__mutmut_1; 1 untested (no test runs the mutated function)
    verdict: refuse
    $ echo $?
    1

`halter HEAD --baseline HEAD` prints `verdict: nothing to audit` and exits
3; `halter no-such-rev` prints `error: cannot check out revision
'no-such-rev': ...` on stderr and exits 2.

## Developing

    python3 -m venv .venv && .venv/bin/pip install -e . pytest
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q

The checks shell out to `ruff`, `coverage` and `mutmut` by name, hence the
`PATH`. The test suite stubs `mutmut` and never runs real mutants.

## License

Copyright (C) 2026  Eliza Halpern and Eryn Lipkowitz

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public
    License along with this program.  If not, see
    <https://www.gnu.org/licenses/>.
