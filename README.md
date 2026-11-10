# CaneTrust AI: estate document annotation, command line and web

Turns the documents of one matter (a trust and its amendments, a will and codicils, notices,
letters, accountings, in any mix of files) into one annotation workbook for the Office of Estate
and Gift Planning, ready for attorney review. Run it from the command line, or host it on Vercel
so users upload their documents and download the workbook. The design is `docs/DESIGN.md`.

## Architecture

```
 files ─► page_source ─► triage ─► plan ─► (confirm) ─► labels + chains ─┬─► annotate (one call per chain, in parallel) ─┐
          (text layer)   (one call)                                     └─► facts lane (one call) ───────────────────┴─► workbook
```

One pipeline. Branching happens on what triage finds (documents, types, dates), never on how
many files were uploaded. `service.py` is called by the CLI and the web API alike; it works on
bytes in memory and returns the workbook as bytes.

## Files

| File | Role |
|---|---|
| `api/index.py` | FastAPI app: validates uploads, streams progress and heartbeats, returns the workbook in the final event |
| `public/index.html` | The web page, in the UM green and orange style (static; no build step) |
| `service.py` | `load_files()`, `triage_packet()`, `annotate_plan()`, and the one-shot path the CLI and legacy request use |
| `page_reader.py`, `page_source.py` | File bytes to page records; scans are refused (OCR comes later) |
| `page_labels.py` | Printed page labels per document (`printed`, `inferred`, `unnumbered, sheet N of M`) |
| `triage.py`, `triage_schema.py`, `triage_previews.py` | One call over page previews: where each document starts and ends, and what it is |
| `plan.py` | The document map the confirm screen edits; validated again on the server |
| `chains.py` | Pure logic: which instruments amend which, ordered by their own dates |
| `facts.py` | Contacts and court facts from any document, with sources and conflicts |
| `annotation_schema.py`, `annotate.py` | One annotation schema and call per chain (a chain with no amendments is n = 0) |
| `workbook.py` | One workbook per matter: a sheet per chain plus the Packet map |
| `settings.py` | All settings from environment variables; asset paths anchored to the file |
| `assets/` | The template, the annotation and triage rules, the default treatments, the chain families |
| `cli.py`, `config.py` | Command-line entry and its inputs |
| `dev_stub_server.py` | Runs the whole app with a fake model: no key, no cost |
| `test_*.py`, `ui_test/`, `testkit.py` | Tests; none needs an API key |
| `tools/` | Dev tools that make real (paid) calls: `probe_schemas.py`, `live_triage.py` |

## Key decisions

- **Content beats file names.** Triage classifies documents by what they say; chains are ordered by the dates in the documents.
- **Citations use printed page numbers**, never PDF positions. Code checks every cited page exists.
- **Scans are refused with an explanation**, not passed to the model. OCR is not built yet.
- **Uploads are validated before anything streams**, so a bad file gets an immediate HTTP error.
- **Long calls stream progress and a heartbeat** every 10 s; the workbook arrives in the last event.
- **One chain failing does not sink the others**: the workbook gets the rest and the packet map says what failed.
- **The original API contract still works.** A request without `Accept: application/x-ndjson` (one `file`, optional `label` and `status`) gets the workbook bytes back directly.
- **Nothing touches disk.** Uploads and results are in memory; the raw-output debug dump is off unless `CANETRUST_DEBUG_DIR` is set.
- **A deployment with no access code refuses to run.** A public URL that spends money needs a gate.
- **Server logs never contain document text**: error class names and our own messages only.
- **`cli.py`, not `main.py`**: Vercel treats `main.py` as a candidate web entrypoint.

## Run it locally

```bash
pip install -r requirements-dev.txt
python dev_stub_server.py                  # the page at http://127.0.0.1:8000, fake model
```

Real model: put `ANTHROPIC_API_KEY` in `.env`, then `uvicorn api.index:app` (or `vercel dev`).
Command line: list the files in `config.py`, then `python cli.py`. Tests: every `test_*.py`
(see CLAUDE.md for the list), and `cd ui_test && npm install && node test_ui.mjs`.

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
