# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CaneTrust AI turns a trust (plus any amendments) into a filled Excel annotation workbook for the
University of Miami Office of Estate and Gift Planning. It runs as a CLI (`cli.py`) and as a
FastAPI app deployed on Vercel (`api/index.py` + static `public/index.html`, no frontend build).

**Status:** this branch (`unified-pipeline`) is implementing `docs/DESIGN.md`, milestone by milestone
(each ends with passing tests and a commit). The two-path routing (`triage()` by amendment count),
the `[PDF page N of M]` markers, the two templates, and the single-step `/api/annotate` described
below are the current code and are slated for removal; don't build new work on them.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt          # runtime deps + uvicorn/httpx for local dev and tests
.venv/bin/python dev_stub_server.py [--no-browser]     # full web app at http://127.0.0.1:8000 with a FAKE model (no key, no cost)
.venv/bin/uvicorn api.index:app                        # real model; needs ANTHROPIC_API_KEY in .env
.venv/bin/python cli.py                                # CLI; real (paid) model call; inputs in config.py, default to fixtures/; writes OUTPUT/

.venv/bin/python test_multi.py                         # triage + multi-document pipeline + CLI (55 checks)
.venv/bin/python test_web.py                           # extraction + HTTP API (FastAPI TestClient) (102 checks)
.venv/bin/python test_pages.py                         # page reader + labeler + page source (96 checks)
.venv/bin/python test_gay_fixtures.py                  # real-fixture expectations; text checks skip until OCR (140 checks)
.venv/bin/python test_triage.py                        # triage with a stubbed model, invented pages (113 checks)
.venv/bin/python test_chains.py                        # chain building, table-driven (83 checks)
.venv/bin/python test_facts.py                         # facts lane with a stubbed model; reproduces expected_facts.json (40 checks)
.venv/bin/python tools/probe_schemas.py                # REAL (tiny, paid): is each strict tool schema accepted?
.venv/bin/python tools/live_triage.py FILE... [--expected MAP]   # REAL (paid) triage; prints the map, never page text
cd ui_test && npm install && node test_ui.mjs          # drives public/index.html in jsdom against a stub server (44 checks)
```

Use the venv: the `python`/`python3` on PATH is a different interpreter without the dependencies.
The UI test needs `npm install` in `ui_test/` once; it starts `dev_stub_server.py --no-browser` under
`$PYTHON`, else `.venv/bin/python`, else `python3`.

Tests are plain scripts (no pytest) using a local `check()` helper; there is no way to run a single
test besides editing the script. None need an API key or network: they monkeypatch
`service.analyze_document` / `service.analyze_packet` with canned annotations from `testkit.py`.
They are isolated from your `.env`: `CANETRUST_DISABLE_DOTENV=1` makes `settings.load_local_env()` a
no-op, and importing `testkit` replaces `anthropic.Anthropic` so an unstubbed model call raises
`RealModelCallInTests` instead of making a paid call.
`test_multi.py` skips 4 formula-value checks when `/mnt/skills/public/xlsx/scripts/recalc.py` is
absent (55 checks locally, 59 where it exists).
`fixtures/` holds a FICTIONAL "Marlowe" trust packet (original + 4 amendments) and a blank scan-like PDF.
`fixtures/gay/` holds REAL scanned documents (cleared for use) plus hand-written expectations, for
end-to-end acceptance at the end of the build (`docs/ACCEPTANCE.md`). They have no text layer, so
`test_gay_fixtures.py` runs only its structure checks and skips its page-label checks until OCR or a
sidecar exists. It is not an OCR test. Don't edit the expected files just to make a check pass.
`INPUT/`, `OUTPUT/` and `private/` are git-ignored for real documents and results; templates belong in `assets/`.

## Architecture

One in-memory pipeline, two front doors. Both `cli.py` and `api/index.py` call `service.py`, which
takes bytes and returns workbook bytes, so nothing touches disk (Vercel has no durable disk).

- **`service.prepare_packet()`** — fast, no network: `extract.py` (PDF/TXT → text with
  `[PDF page N of M]` markers; scans are refused with `ExtractionError`, not OCR'd) then
  `packet.make_packet()` (validation, size limits). Errors here are user-fixable; the web API runs
  this *before* streaming so bad uploads get an immediate HTTP error.
- **`service.annotate_packet()`** — slow: `packet.triage()` routes deterministically on what the
  user supplied (never on model output):
  - no amendments → **single** pipeline: `schema.py` → `llm.py` → `render.py` + `cell_map.py` → `assets/MiLA_Annotation_Template_v0.xlsx`, rules `assets/extraction_rules.md`
  - any amendments → **multi** pipeline: `schema_multi.py` → `llm_multi.py` → `render_multi.py` + `cell_map_multi.py` → `assets/MiLA_Annotation_Template_Amendments_v0.xlsx`, rules `assets/extraction_rules_multi.md`

Cross-file coupling to be aware of:
- `schema_multi.py` builds its per-field `FieldHistory` models from the single-document models in
  `schema.py` (`_history_version`), so field changes in `schema.py` propagate to both pipelines,
  and to the hard-coded row anchors in `cell_map*.py` and the canned data in `testkit.py`.
- `cell_map*.py` are row/cell anchors built by inspecting the template xlsx files. If a template
  changes, update the cell map; renderers insert rows when variable-length sections overflow.
- Pydantic schemas double as the model's tool schema; field-level instructions live in the schema
  descriptions, while the rules `.md` files are the (prompt-cached) system prompt.
- `packet.MAX_AMENDMENTS = 8` matches the amendment columns in the amendments template.
  Missing amendment numbers (e.g. 4 supplied, 3 not) make affected operative answers provisional;
  `schema_multi.reconcile_with_packet()` aligns model output with the supplied amendment numbers.
- `settings.py` is the only place config is read (env vars with defaults; `.env` loaded locally,
  never on Vercel). Asset paths are anchored to the file so they resolve in the serverless bundle.

### Unified pipeline (in progress, `docs/DESIGN.md`)

- Milestone 1: `page_reader.read_pages()` turns file bytes into `PageRecord`s (`position` is for
  ordering only and is never a label); `page_labels.label_document()` labels ONE document's
  pages from printed numbers (`printed` / `inferred` / `unnumbered, sheet N of M`). Its rules
  are in the `page_labels.py` docstring. Warnings must never contain document text.
- `page_reader.py` repeats the PDF checks in `extract.py` on purpose; the old modules go away in
  milestone 6. Don't wire the new modules into `service.py` before then.
- Milestone 2: `page_source.load_pages()` is the only way to get pages: text layer first, then
  (tests and dev tools only, when `sidecar_dir` is passed) `<filename>.pages.json`, a JSON list of
  page texts in sheet order, which is also the format the future OCR step must produce. The API
  never passes `sidecar_dir`; `test_pages.py` checks that `api/index.py` and `service.py` never mention it.
- Milestone 3: `triage.triage(pages_by_file, settings, client=None)` makes one `record_triage` call per
  window over `triage_previews.py` previews (first 2 / last 6 lines + code-computed features), then
  validates (retry once, then conservative repair + `needs_attention`), applies
  `assets/default_treatments.json` (authoritative; a blank form is `log_only`), labels each document,
  and adds cross-check flags. Schema in `triage_schema.py` (strict: all fields required, "unknown" for
  null, no maxItems/maxLength, so evidence limits are trimmed in code); prompt in `assets/triage_rules.md`.
  Forced `tool_choice` is used except for models that reject it (`NO_FORCED_TOOL_CHOICE`). Warnings,
  flags and retry problems carry ids, sheets, counts and enum values only, never page or model text.
  `schema_limits.strict_schema_problems()` (re-exported by testkit) checks any tool schema against the
  strict-mode limits; only the real API (`tools/probe_schemas.py`) can say if a grammar is too large.
- Milestone 4: `chains.build_chains(ChainDoc list)` is pure logic over `assets/chain_families.json`; see its
  docstring for the ordering, restatement, revocation and gap rules. Triage records `revokes[]`.
- Milestone 5: `facts.extract_facts()` is one strict `record_facts` call (triage model and effort) over
  facts_only documents in full plus contact-cue pages of the others; code fills source dates from triage and
  `merge_facts()` keeps conflicts (newest wins). Facts only ever become contact rows (`contact_rows()`).

### Web API (`api/index.py`)

- `POST /api/annotate`: with `Accept: application/x-ndjson` it streams progress events plus a
  heartbeat every 10 s, and the workbook arrives base64 in the final event. Without that header it
  keeps the original contract (one `file`, optional `label`/`status`) and returns xlsx bytes directly
  — don't break this.
- Access code is required (`CANETRUST_ACCESS_CODE`, via `x-access-code` header or form field). With
  no code set the app answers 503 unless `CANETRUST_ALLOW_OPEN=1`.
- `GET /api/health` reports `assets_present`, `api_key_set`, etc.

## Constraints and invariants

- **Never log document text.** Server logs contain only exception class names and our own messages.
- Raw model output is only written to disk when `CANETRUST_DEBUG_DIR` is set.
- Vercel limits: 4.5 MB request body (app caps uploads at 4.0 MB), `maxDuration` 300 s in
  `vercel.json`; keep `CANETRUST_DEADLINE_SECONDS` (default 280) below it. `vercel.json`
  `excludeFiles` keeps tests, fixtures, CLI and dev files out of the deployed bundle — update it
  when adding non-runtime files.
- The CLI is `cli.py`, not `main.py`, because Vercel treats `main.py` as a candidate entrypoint.
- Keep `requirements.txt` (runtime, deployed) separate from `requirements-dev.txt` (local/tests).
- Default model is `claude-sonnet-4-6` (`CANETRUST_MODEL`); other knobs: `CANETRUST_EFFORT`,
  `CANETRUST_MAX_TOKENS_SINGLE/MULTI`, `CANETRUST_MAX_PAGES`, `CANETRUST_MAX_CHARS` (see `settings.py`).
