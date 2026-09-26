# Changelog

## 0.1.2 (2026-09-26)

Brings `halter --tiered` to parity with `saddle audit --tiered` as of saddle
phase2-check 4a4b60d. The default mode (no `--tiered`) gives the same
verdict, report and JSON as 0.1.1.

- **New:** `--tiered` runs the checks as tier 0 (per changed file: syntax,
  ruff, imports), tier 1 (tests, coverage, dead-code, public-deletions,
  node-scope, target-scope, assertion-preservation) and tier 2 (mutation,
  property-coverage, red-phase, requirement-binding, full-suite), with its
  own text report and JSON shape (`verdict`, `tiers[].findings[]` with
  `gate`, `tier`, `verdict`, `reason`, `detail`, `cites`). Exit codes are
  unchanged: 0 accept, 1 refuse, 2 could not audit, 3 nothing to audit.
- **Tightened (tiered mode only):** a new check, `imports`, refuses a
  changed file whose absolute imports do not resolve to the standard
  library, the tree, or halter's own interpreter.
- **Scope narrowed (tiered mode only):** tier 2 is not run on a tree whose
  tier 1 failed; it reports one `blocked` finding naming the failed tier-1
  checks. The verdict is `refuse` either way.
- **Scope narrowed (library, opt-in):** `runner.run_node_gate(...,
  tier2=False)` skips the mutation run, the property oracle and all but
  one red-phase baseline sample. The default, `tier2=True`, is unchanged.
- **Tightened (cache):** the check surface now also hashes
  `halter/auditor.py`, so verdicts cached by 0.1.1 are not served by 0.1.2
  (they miss once and are recomputed).
- **No contract change:** `audit.py`'s staging and check conversion are
  split into `staged_copy`, `baseline_tree` and `audit_checks`, shared by
  both modes.
- **Credits (metadata only; no contract change):** package metadata,
  README and a new CITATION.cff name the authors as Eliza Halpern and
  Eryn Lipkowitz.
- **Tests (no contract change):** 124 direct unit tests of the 13 gate
  predicates in `halter.gates` (`check_*`), ported from saddle's
  `tests/test_gates.py` at phase2-check 4a4b60d, with the five diff and
  text fixtures they read. Each gate now has at least one known-good
  instance that passes and one known-bad instance that fails. The 24
  tests of saddle's planner checks, which halter does not ship, and 16
  tests that exercise only `run_tier1` or other helpers are not ported.

Not in this release (waiting on saddle's in-progress change to tier 2's
mutation verdict): a survivor shortlist on the mutation finding, per-survivor
detail in the JSON, and any change to how the mutation kill rate decides.
