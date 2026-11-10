# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CaneTrust AI turns the documents of one estate matter (a trust and its amendments, a will and
codicils, notices, letters, accountings, in any mix of files) into one Excel annotation workbook for
the University of Miami Office of Estate and Gift Planning. It runs as a CLI (`cli.py`) and as a
FastAPI app deployed on Vercel (`api/index.py` + static `public/index.html`, no frontend build).

**Status:** this branch (`unified-pipeline`) implements `docs/DESIGN.md`. Progress, decisions taken by
default, and what is next are in `docs/PROGRESS.md`; read it first when resuming. OCR (milestone 9)
is not built: scanned PDFs are refused.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt          # runtime deps + uvicorn/httpx for local dev and tests
.venv/bin/python dev_stub_server.py [--no-browser]     # full web app at http://127.0.0.1:8000 with a FAKE model (no key, no cost)
.venv/bin/uvicorn api.index:app                        # real model; needs ANTHROPIC_API_KEY in .env
.venv/bin/python cli.py                                # CLI; real (paid) model calls; files in config.py (default fixtures/); writes OUTPUT/

.venv/bin/python test_pages.py                         # page reader + labeler + page source (97 checks)
.venv/bin/python test_gay_fixtures.py                  # real-fixture expectations; text checks skip until OCR (140 checks)
.venv/bin/python test_triage.py                        # triage with a stubbed model, invented pages (113 checks)
.venv/bin/python test_chains.py                        # chain building, table-driven (83 checks)
.venv/bin/python test_facts.py                         # facts lane; reproduces expected_facts.json (40 checks)
.venv/bin/python test_annotate.py                      # annotation schema, request, cited-page check, parallel chains (47 checks)
.venv/bin/python test_workbook.py                      # template rendering, chain sheets, packet map, tallies (77 checks)
.venv/bin/python test_pipeline.py                      # end to end with stubbed calls; plan validation; CLI (64 checks)
.venv/bin/python test_web.py                           # page reading + HTTP API, both steps and the legacy request (123 checks)
cd ui_test && npm install && node test_ui.mjs          # drives public/index.html (drop zone, confirm screen) in jsdom against a stub server (58 checks)

.venv/bin/python tools/probe_schemas.py                # REAL (tiny, paid): does the API accept each tool schema?
.venv/bin/python tools/live_triage.py FILE... [--expected MAP]   # REAL (paid) triage; prints the map, never page text
```

Use the venv: the `python`/`python3` on PATH is a different interpreter without the dependencies.
The UI test needs `npm install` in `ui_test/` once; it starts `dev_stub_server.py --no-browser` under
`$PYTHON`, else `.venv/bin/python`, else `python3`.

Tests are plain scripts (no pytest) using a local `check()` helper; there is no way to run a single
test besides editing the script. None need an API key or network: `testkit.StubModel` stands in for
the Anthropic client and answers `record_triage`, `record_facts` and `analyze_documents` from the
request itself (or from per-test overrides); `testkit.install_stub(service)` routes every call the
service makes to it, so the real code runs end to end. They are isolated from your `.env`:
`CANETRUST_DISABLE_DOTENV=1` makes `settings.load_local_env()` a no-op, and importing `testkit`
replaces `anthropic.Anthropic` so an unstubbed model call raises `RealModelCallInTests` instead of
making a paid call. `testkit.eval_tally()` computes the workbook's COUNTIF/COUNTBLANK tallies.
`fixtures/` holds a FICTIONAL "Marlowe" trust packet (original + 4 amendments, the third a
restatement) and a blank scan-like PDF.
`fixtures/gay/` holds REAL scanned documents (cleared for use) plus hand-written expectations, for
end-to-end acceptance at the end of the build (`docs/ACCEPTANCE.md`). They have no text layer, so
`test_gay_fixtures.py` runs only its structure checks and skips its page-label checks until OCR or a
sidecar exists. It is not an OCR test. Don't edit the expected files just to make a check pass.
`INPUT/`, `OUTPUT/`, `private/` and `scratchpad/` are git-ignored for real documents and results;
templates belong in `assets/`.

## Architecture

One in-memory pipeline. `cli.py` and `api/index.py` both call `service.py`, which takes bytes and
returns workbook bytes, so nothing touches disk (Vercel has no durable disk). Branching happens on
what triage finds, never on how many files were uploaded.

1. **`service.load_files()`**: fast, no network. `page_source.load_pages()` is the only way to get
   pages: the text layer via `page_reader.read_pages()` (scans refused with `ExtractionError`), then,
   for tests and dev tools only when `sidecar_dir` is passed, `<filename>.pages.json` (a JSON list of
   page texts in sheet order: the format the future OCR step must produce). The API never passes
   `sidecar_dir`. `PageRecord.position` orders pages and is never a label. Errors here are the user's
   to fix; the API runs this before streaming.
2. **`service.triage_packet()`**: `triage.triage()` makes one strict `record_triage` call per window over
   `triage_previews.py` previews (first 2 / last 6 lines + code-computed features), validates (retry
   once, then conservative repair + `needs_attention`), applies `assets/default_treatments.json`
   (authoritative; a blank form is `log_only`), labels each document, adds cross-check flags, and
   records `refers_to[]` and `revokes[]`. The result becomes a `plan.Plan`.
3. **Confirm** (a person, or auto-confirm in the CLI and the legacy request): `plan.validate_plan()`
   re-checks any plan that comes back from a client (coverage, ranges, types, treatments, overrides).
4. **`service.annotate_plan()`**: `plan.label_plan()` recomputes printed labels per document
   (`page_labels.label_document()`; rules in its docstring), `plan.chain_plan()` runs
   `chains.build_chains()` (pure logic over `assets/chain_families.json`; rules in its docstring),
   annotated documents that cannot be chained become single-instrument chains, then every chain is
   annotated in parallel (`annotate.annotate_chains()`) beside the facts lane (`facts.extract_facts()`),
   and `workbook.build_workbook()` assembles one workbook.

- **Annotation** (`annotate.py`, `annotation_schema.py`, `assets/annotation_rules.md`): one call per chain.
  The schema is the multi-document history form (`original` / `amendments[]` / `operative`); a chain
  with no amendments is n = 0 (`operative` null, copied from `original` by `reconcile()`). The model
  reads `[Page <label>]` markers from the labeler, every instrument tagged (`Trust`/`Will`/`Instrument`,
  `Amendment N`/`Codicil N`) with its status (BASE, ACTIVE, REVOKED by ..., revival assumed).
  `check_citations()` flags any cited page label that is not a page of the instrument it names. The tool
  is sent WITHOUT `strict`: the API rejects this schema's grammar as too large (`tools/probe_schemas.py`).
  The single-instrument field models in `annotation_schema.py` carry the field instructions;
  `workbook.FIXED_ROWS` and `testkit.chain_annotation()` follow the same field names.
- **Facts** (`facts.py`): one strict `record_facts` call (triage model and effort) over facts_only
  documents in full plus contact-cue pages of the others; code fills source dates from triage;
  `merge_facts()` keeps conflicts (newest wins). Facts only ever become contact rows.
- **Workbook** (`workbook.py`, `assets/MiLA_Annotation_Template.xlsx`): Instructions once, a "Packet map"
  sheet, one sheet per annotated chain copied from the template's Annotation sheet (copy_worksheet loses
  validations, freeze panes and print titles; `restore_sheet_machinery()` and the renderer re-apply them).
  Cell anchors are constants at the top of `workbook.py`; if the template changes, change them.
  Eight amendment columns (`AMENDMENT_SLOTS`); a longer chain is refused. Section 10 holds contacts.
- Tool schemas are pydantic models built on `schema_limits.StrictModel`; field-level instructions live
  in the schema descriptions, the rules `.md` files are the (prompt-cached) system prompts.
  `schema_limits.strict_schema_problems()` checks a schema against the strict-mode limits; only the real
  API can say whether a grammar is too large. Forced `tool_choice` is used except for models that reject
  it (`triage.NO_FORCED_TOOL_CHOICE`).
- `settings.py` is the only place config is read (env vars with defaults; `.env` loaded locally,
  never on Vercel). Asset paths are anchored to the file so they resolve in the serverless bundle.

### Web API (`api/index.py`) and page (`public/index.html`)

Two requests, never one held open while a person decides. The browser holds the state between
them (plan JSON + page texts, capped at `CANETRUST_MAX_STATE_MB`, default 3 MB of page text).

- `POST /api/triage` (multipart `files`, `access_code`, optional `doc_name`): size, type and page
  text are checked before streaming; then NDJSON progress + heartbeat every 10 s, ending with
  `{"type":"triaged","plan":..., "pages":{file_id:[texts]}, "skip_confirm":bool}`.
- `POST /api/annotate` with `Content-Type: application/json` (`plan`, `pages`, `access_code`): the
  server re-validates the plan (`plan.validate_plan`, `service.confirmed_plan_problems`: coverage,
  ranges, types, treatments, overrides, at most 8 amendments per chain) and refuses with 422
  `invalid_plan` + `problems`; labels and chains are recomputed; then NDJSON ending in
  `{"type":"done", ..., "xlsx_base64":...}`.
- `POST /api/annotate` multipart is the legacy request: one `file` (or `trust`, plus optional
  `amendment_files`), `label`, `status`. It runs `service.annotate_one_shot` (triage, auto-confirm,
  annotate). With `Accept: application/x-ndjson` it streams; without, it returns xlsx bytes directly
  — don't break this. If triage needs a person it answers `needs_confirmation` (409 in classic mode).
- The page: one multi-file drop zone, then the confirm screen (files; documents in file order with
  those needing a decision first; type, date, treatment, chain (amends), note, superseded, exclude,
  split, merge; a live summary; a gate when triage suspects two matters). Every edit is recorded in
  `plan.edits` and written to the packet map as an audit record. The screen is skipped only for one
  high-confidence document with no flags (`service.can_skip_confirm`).
- Access code is required (`CANETRUST_ACCESS_CODE`, via `x-access-code` header or form field). With
  no code set the app answers 503 unless `CANETRUST_ALLOW_OPEN=1`.
- `GET /api/health` reports `assets_present`, `api_key_set`, etc.

## Constraints and invariants

- **Never log document text.** Server logs contain only exception class names and our own messages.
  Warnings, flags, errors and progress carry ids, sheets, counts and enum values, never page or model text.
- Raw model output is only written to disk when `CANETRUST_DEBUG_DIR` is set.
- Vercel limits: 4.5 MB request body (app caps uploads at 4.0 MB), `maxDuration` 300 s in
  `vercel.json`; keep `CANETRUST_DEADLINE_SECONDS` (default 280) below it. `vercel.json`
  `excludeFiles` keeps tests, fixtures, tools, docs, CLI and dev files out of the deployed bundle —
  update it when adding non-runtime files.
- The CLI is `cli.py`, not `main.py`, because Vercel treats `main.py` as a candidate entrypoint.
- Keep `requirements.txt` (runtime, deployed) separate from `requirements-dev.txt` (local/tests).
- Default model is `claude-sonnet-4-6` (`CANETRUST_MODEL`); other knobs: `CANETRUST_EFFORT`,
  `CANETRUST_MAX_TOKENS_ANNOTATE`, `CANETRUST_MAX_PAGES`, `CANETRUST_MAX_CHARS`, the triage and facts
  settings (see `settings.py`).
