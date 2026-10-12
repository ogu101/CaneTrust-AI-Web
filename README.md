# CaneTrust AI: trust annotation, command line and web

Turns a trust (and its amendments, if any) into a filled annotation workbook for the Office of
Estate and Gift Planning, ready for attorney review. Run it from the command line, or host it
on Vercel so users upload their documents and download the workbook.

## Architecture

```
 config.py ──► cli.py ──────────────┐                       ┌── no amendments ──► single pipeline ─► v0 template
 (paths on disk)                    ├─► service.py ─► triage┤
 browser ─► public/index.html ─► api/index.py ─┘            └── any amendments ─► multi pipeline ──► amendments template
 (uploads)    (FastAPI, streams progress)
```

One pipeline, two front doors. `service.annotate_packet()` is called by both, works on bytes in
memory, and returns the workbook as bytes, so the CLI and the web app cannot behave differently.

## Files

| File | Role |
|---|---|
| `api/index.py` | FastAPI app: validates uploads, streams progress, returns the workbook in the final event |
| `public/index.html` | Upload page, in the existing UM green and orange style (static; no build step) |
| `service.py` | `prepare_packet()` (extract and validate) and `annotate_packet()` (model call and workbook) |
| `extract.py` | PDF/TXT to text, in memory. Refuses scans, marks unreadable pages, adds `[PDF page N of M]` markers |
| `packet.py` | `Packet`, validation, `triage()`; `make_packet()` (memory) and `build_packet()` (paths) |
| `settings.py` | All settings from environment variables; asset paths anchored to the file |
| `schema*.py`, `llm*.py`, `render*.py`, `cell_map*.py` | Single-document and multi-document pipelines |
| `assets/` | The two templates and two rules files, bundled with the deployment |
| `cli.py`, `config.py` | Command-line entry and its inputs (this was `main.py`) |
| `dev_stub_server.py` | Runs the whole app with a fake model: no key, no cost |
| `test_multi.py`, `test_web.py`, `ui_test/` | Tests; none needs an API key |

## Key decisions

- **Extraction is part of the pipeline.** `config.py` and the upload form both take PDFs directly.
- **Scans are refused with an explanation**, not passed to the model. OCR is not built.
- **Uploads are validated before anything streams**, so a bad file gets an immediate HTTP error.
- **The model call streams progress and a heartbeat** every 10 s; the workbook arrives in the last event.
- **The original API contract still works.** A request without `Accept: application/x-ndjson` (one `file`, optional `label` and `status`) gets the workbook bytes back directly, as the first version of `/api/annotate` did. The new page asks for the stream.
- **Nothing touches disk.** Uploads and results are in memory; the raw-output debug dump is off unless `CANETRUST_DEBUG_DIR` is set.
- **A deployment with no access code refuses to run.** A public URL that spends money needs a gate.
- **Server logs never contain document text**: error class names and our own messages only.
- **`cli.py`, not `main.py`**: Vercel treats `main.py` as a candidate web entrypoint. The existing repo routes `api/index.py` explicitly, so this rename is optional.

## Adding this to the existing repo

Do it on a branch so Vercel builds a preview and `main` stays as it is.

| Action | Files |
|---|---|
| Replace | `api/index.py`, `public/index.html`, `config.py`, `vercel.json`, `requirements.txt`, `.gitignore` |
| Replace (small edits) | `llm.py` (debug dump is opt-in), `render.py` (rows shift correctly, writes to memory, status string), `cell_map.py` (adds `STATUS_TO_CELL`) |
| Unchanged | `schema.py` |
| Add | `extract.py`, `packet.py`, `service.py`, `settings.py`, `workbook_io.py`, `schema_multi.py`, `llm_multi.py`, `render_multi.py`, `cell_map_multi.py`, `cli.py`, `assets/` (both templates and both rules files), `dev_stub_server.py`, `test_multi.py`, `test_web.py`, `testkit.py`, `fixtures/`, `ui_test/` |
| Delete | `main.py` (now `cli.py`), `extract_from_pdf` (now `extract.py`), root `extraction_rules.md` and `INPUT/MiLA_Annotation_Template_v0.xlsx` (now in `assets/`), `test_render.py` (superseded by `test_multi.py`) |

`vercel.json` keeps the repo's `/api/(.*)` rewrite and `maxDuration` of 300, and adds `excludeFiles`.
After the first deploy, set `CANETRUST_ACCESS_CODE` in the project's environment variables, or the
app answers 503 (a deployment with no code refuses to run). `ANTHROPIC_API_KEY` should already be set.

## Run it locally

```bash
pip install -r requirements-dev.txt
python dev_stub_server.py                  # the page at http://127.0.0.1:8000, fake model
```

Real model: put `ANTHROPIC_API_KEY` in `.env`, then `uvicorn api.index:app` (or `vercel dev`).
Command line: set `config.py`, then `python cli.py`. Tests: `python test_multi.py`,
`python test_web.py`, and `cd ui_test && npm install && node test_ui.mjs`.

## Deploy to Vercel

1. Push the project (not `canetrust_ai_venv/`, `INPUT/`, `OUTPUT/`; the ignore files cover them).
2. Set environment variables in the project settings:

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | required |
| `CANETRUST_ACCESS_CODE` | required: the shared code users type into the page |
| `CANETRUST_DEADLINE_SECONDS` | default 280; keep it below `maxDuration` |
| `CANETRUST_MODEL`, `CANETRUST_EFFORT`, `CANETRUST_MAX_TOKENS_MULTI` | optional tuning |

3. Check `GET /api/health` after deploying: `assets_present` and `api_key_set` should be true.

## Limits that come from Vercel

- **Request and response bodies: 4.5 MB.** The app caps one upload at 4.0 MB total.
- **Function duration: 300 s on Hobby; up to 800 s on Pro.** `vercel.json` sets 300. On Pro, set
  `maxDuration` to 800 and `CANETRUST_DEADLINE_SECONDS` to about 780.
- **Python bundle: 500 MB.** Only runtime files are bundled (see `excludeFiles`).

## Known limitations

- Scanned PDFs are not supported yet, so many real documents will be refused.
- Not yet deployed: run `vercel dev` and a preview deployment before relying on it.
- The multi-document model call has not been timed against the live API; it may exceed 300 s.
- Documents are sent to the model provider in full; there is no confidentiality routing.
