# Build progress (unified pipeline)

Resume from the last section. Phases follow the build prompt (0 clean start, 1 chains, 2 facts,
3 unified annotation and workbook, 4 service/API/UI, 5 deploy). DESIGN.md milestones 4-8 map to
phases 1-5.

## Live (paid) calls used

| When | Tool | Calls | Input tokens | Output tokens |
|---|---|---|---|---|
| Phase 0 | tools/probe_schemas.py (triage, single, multi) | 3 | 4,765 | 128 |

## Phase 0: clean start (done)

- Suites before any change: test_multi 55, test_web 102, test_pages 96, test_gay_fixtures 140
  structure (9 text checks skipped), test_triage 107, UI 44. All green.
- `git ls-files INPUT OUTPUT private .env scratchpad` and the INPUT/OUTPUT log are empty: no
  real document or secret was ever committed.
- .gitignore: added `.DS_Store` and `scratchpad/` (`ui_test/node_modules/` was already there).
- Commits: housekeeping (test isolation, blocked real clients), milestones 1-2, milestone 3.
  testkit.py and settings.py were split between commits by staging intermediate versions.
- `schema_limits.py`: the strict-schema counter moved out of testkit.py (which blocks the real
  client on import) so tools can use it; testkit re-exports it.
- `tools/probe_schemas.py` run against claude-sonnet-4-6:
  - triage (record_triage): accepted (optional 0, union 0).
  - single-document: accepted (optional 10, union 5).
  - multi-document: **rejected**, HTTP 400 "The compiled grammar is too large, which would cause
    performance issues. Simplify your tool schemas or reduce the number of strict tools." Our
    counts (optional 5, union 5) are within the documented limits; the limit hit is grammar
    size, which the local checker cannot see.

### Decisions

- D0.1 (default rule): the multi-document schema is rejected in strict mode, and the unified
  annotation schema is built from it, so the unified annotation call is sent WITHOUT `strict`.
  Code validates instead: pydantic validation of the tool input, reconciliation of per-instrument
  entries, and the cited-page check. Recorded in DESIGN.md.
- D0.2: the gay PDFs are committed (they are cleared fixtures and the plan names them in commit
  b). The repo's visibility could not be checked (no `gh` CLI); check it before pushing.

## Phase 1: chains (done)

- triage: `revokes[]` (what, date) in the strict schema, the rules, validation (unparseable date
  is a retry problem) and `TriagedDocument.revokes`. Triage tests 107 -> 113.
- `assets/chain_families.json`, `chains.py` (`ChainDoc`, `build_chains`, `from_triaged`),
  `test_chains.py` (83 checks, 22 table cases plus expected_map's chains).
- Suites: multi 55, web 102, pages 96, gay 140 (+9 skipped), triage 113, chains 83, UI 44.

### Decisions

- D1.1: annotated documents that cannot be chained (no date, other_dispositive) are returned as
  `standalone` and will be annotated on their own sheet, flagged; nothing is silently dropped.
- D1.2: two originals in one group: the latest is the base and the earlier is superseded, flagged
  `multiple_bases` (wills customarily revoke earlier wills; for trusts a person must confirm).
- D1.3: an unknown principal forms its own group, flagged `principal_unknown`.
- D1.4: an override `amends` with no date is placed last in its target's chain, flagged
  `order_assumed`; an override to an unchained target is ignored, flagged.
- D1.5: revocations are matched by date within the chain; an unmatched one is flagged.
- D1.6: flags carry ids and counts; gap and superseded-reference descriptions and dates are kept
  in the chain for the workbook only.

## Next

Phase 2: facts lane (facts.py, stubbed tests reproducing expected_facts.json).
