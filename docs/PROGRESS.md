# Build progress (unified pipeline)

Resume from the last section. Phases follow the build prompt (0 clean start, 1 chains, 2 facts,
3 unified annotation and workbook, 4 service/API/UI, 5 deploy). DESIGN.md milestones 4-8 map to
phases 1-5.

## Live (paid) calls used

| When | Tool | Calls | Input tokens | Output tokens |
|---|---|---|---|---|
| Phase 0 | tools/probe_schemas.py (triage, single, multi) | 3 | 4,765 | 128 |
| Phase 3 | tools/probe_schemas.py (triage, facts, annotation, annotation strict trial) | 4 | 7,300 | 192 |

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

## Phase 2: facts lane (done)

- `facts.py`: `FactItem` / `FactsAnswer` strict schema (`record_facts`), `build_message`,
  `extract_facts`, `merge_facts`, `contact_rows`. Settings `CANETRUST_FACTS_MAX_TOKENS` (8000)
  and `CANETRUST_FACTS_MAX_CHARS` (150000).
- `test_facts.py` (40 checks): reproduces expected_facts.json from a stub answer, including the
  address conflict (accounting wins, both shown) and no personal phone or e-mail for the
  representative.

### Decisions

- D2.1: the facts call uses the triage model and triage effort (small, cheap call); it is not
  retried. An invalid item is dropped with a warning; no tool call means no facts and a warning.
- D2.2: log_only documents still contribute contact pages (the accounting is a fact source).
- D2.3: successor trustees are two fields (`successor_trustee`, `backup_successor_trustee`) so a
  first and a backup trustee are not mistaken for a conflict.
- D2.4: contacts are tied to a person through `subject`, compared like principals; contacts of
  nobody named go to an "Other contacts" row.

## Phase 3: unified annotation and workbook (done)

Built:
- `annotation_schema.py` (one schema; n = 0 has `operative` null), `assets/annotation_rules.md`
  (one rules file: chains, families, statuses, printed labels, wills and pour-overs).
- `annotate.py`: `chain_input`, `render_chain` ([Page <label>] markers), `build_request`,
  `check_citations`, `annotate_chain`, `annotate_chains` (parallel, failures isolated).
- `workbook.py`: cell anchors, chain sheet renderer, sheet names, packet map, `build_workbook`.
- `plan.py`: `Plan`, `plan_from_triage`, `validate_plan`, `label_plan`, `chain_plan`.
- `service.py`: `load_files`, `triage_packet`, `annotate_plan`, `annotate_one_shot`,
  `describe_failure`, `model_client` (the one place a real client is made).
- CLI (`cli.py`, `config.py` FILES list) and the current API use the same functions with
  auto-confirm. `dev_stub_server.py` uses `testkit.StubModel`.
- Template renamed to `assets/MiLA_Annotation_Template.xlsx`. extract.py folded into
  page_reader.py (ExtractionError, SUPPORTED_SUFFIXES, unreadable-sheet warnings).
- Probe (real): triage and facts accepted strict; annotation accepted non-strict; its strict
  trial rejected again (grammar too large). D0.1 stands.
- Suites: pages 97, gay 140 (+9 skipped), triage 113, chains 83, facts 40, annotate 47,
  workbook 77, pipeline 64, web 88, UI 44.

Deleted modules: schema.py, llm.py, render.py, cell_map.py, schema_multi.py, llm_multi.py,
render_multi.py, cell_map_multi.py, extract.py, packet.py, workbook_io.py,
assets/MiLA_Annotation_Template_v0.xlsx, assets/extraction_rules.md, extraction_rules_multi.md.

Ported test coverage (test_multi.py deleted; its checks moved):
- reconcile order / visible "Could not determine" / warnings, enum casing -> test_annotate.
- rendering with overflow in every section, headers with form and revocation, hidden columns,
  section shifts, dropdown ranges, tally formula and its values (now computed by
  testkit.eval_tally, so the 4 recalc.py-dependent checks always run), header row height,
  BytesIO output, Ambiguous dropdown string -> test_workbook.
- request strict/forced (now: forced, not strict, by decision), reading order, gap stated to the
  model -> test_annotate. CLI routing -> test_pipeline (CLI section).
- test_web extraction checks -> page_reader checks in test_web; API checks rerouted to the stub.

Checks deleted because their subject no longer exists:
- test_multi: "no amendments -> single", "one amendment -> multi", single/multi routing in the
  CLI, "single pipeline used the v0 template", "single renderer writes a workbook" (no routing,
  no second template, no single renderer).
- test_multi: amendment numbers sorted, missing number detected, duplicate / zero / missing-file /
  incomplete-entry / too-many amendment entries rejected, empty trust file rejected (no
  user-numbered amendments; order comes from document dates, coverage from plan validation; the
  8-amendment limit is now an annotate_chain refusal, tested in test_annotate; an empty file is
  still refused by page_reader, tested in test_web).
- test_web: "[PDF page N of M] markers inserted / at the real boundary / can be turned off" and
  "an unreadable page is marked in the text" (markers removed; the page boundary and the
  "no extractable text" marker are now checked in test_web and test_annotate).
- test_web: amendments_meta count / duplicate / non-numeric / malformed / >8 checks, "a gap in the
  amendment numbers is announced" (amendments_meta is accepted and ignored: triage reads content).
- test_web: "status reaches the pipeline", "custom status honoured", "status defaults to
  OPERATIVE" (status now comes from the chain; the legacy field is accepted and ignored; a
  check that a custom status is still accepted remains).
- test_pages: "service.py never mentions page_source" (milestone 6 wired it in on purpose; the
  check that neither the API nor the service ever reads sidecars remains).
- UI test: progress strings and download names of the two old routes were updated, not deleted.

### Decisions

- D3.1: the annotation tool is non-strict (D0.1). Validation failures are not retried: a retry
  of a long call would push the request past Vercel's 300 s limit. The chain fails visibly.
- D3.2: n = 0 lets `operative` be null and copies `original` in code, instead of making the
  model repeat every answer.
- D3.3: instrument tags are `Trust` / `Will` / `Instrument` for the base and `Amendment N` /
  `Codicil N` for the rest; citations name the tag so code can check the page label.
- D3.4: status tallies use exactly the sections 1-5 status cells (SUM of COUNTIF over ranges),
  replacing the template's D12:D200, so contacts and empty rows never distort them.
- D3.5: contacts are section 10 of every chain sheet (duplicated per chain, so each sheet stands
  alone); with no chain sheets they go on the packet map.
- D3.6: sheet order Instructions, Packet map, then chains.
- D3.7: if every chain fails the request fails with the first error; if some fail, the workbook
  lists the failures. A failed facts lane leaves a note and never fails the run.
- D3.8: the one-shot path refuses (needs_confirmation) when triage repaired its answer or flags
  possible multiple matters.
- D3.9: matter name: doc_name, else the legacy trust_label or label, else the first file's stem.
- D3.10: the legacy `status` and `amendments_meta` fields are accepted and ignored.
- D3.11: the packet-level character limit is 2 x CANETRUST_MAX_CHARS; each chain is held to
  CANETRUST_MAX_CHARS. CANETRUST_MAX_TOKENS_ANNOTATE replaces SINGLE/MULTI (MULTI still read).
- D3.12: a chain over 8 amendments is refused per chain (the workbook has 8 columns).

## Next

Phase 4: two-step API (/api/triage, /api/annotate with a confirmed plan), confirm screen UI.
