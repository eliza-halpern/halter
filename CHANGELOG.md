# Changelog

## 0.1.2 (draft, unreleased)

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

Not in this release (waiting on saddle's in-progress change to tier 2's
mutation verdict): a survivor shortlist on the mutation finding, per-survivor
detail in the JSON, and any change to how the mutation kill rate decides.
