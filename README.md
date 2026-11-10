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

Work on a branch: pushing it makes Vercel build a preview, and `main` (production) stays as it is.

1. Check the bundle locally: `python tools/check_bundle.py`. It applies `vercel.json`'s
   `excludeFiles` to the tracked files, confirms `assets/` and the template ship, imports the app
   from a read-only copy of exactly the bundle with `VERCEL=1`, and checks `requirements.txt`
   against the bundle's imports. It makes no network call.
2. Push the branch (`git push -u origin <branch>`). Vercel builds a preview. `INPUT/`, `OUTPUT/`,
   `private/`, `scratchpad/` and `.env` are git-ignored, so they never reach it.
3. Environment variables (project settings, applied to Preview as well as Production):

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | required |
| `CANETRUST_ACCESS_CODE` | required: the shared code users type into the page (without it the app answers 503) |
| `CANETRUST_DEADLINE_SECONDS` | default 280; keep it below `maxDuration` (300) |
| `CANETRUST_MODEL`, `CANETRUST_EFFORT`, `CANETRUST_MAX_TOKENS_ANNOTATE` | optional tuning; lower `CANETRUST_EFFORT` (e.g. `medium`) if long chains hit the deadline |
| `CANETRUST_MAX_STATE_MB` | default 3.0 MB of page text held by the browser between the two steps |

4. Test the preview by hand, following `docs/VERCEL_TEST.md`: health, the Marlowe set through the
   confirm screen, a scan refused, a wrong access code, a file over 4 MB, and the function logs.

`vercel.json` keeps the `/api/(.*)` rewrite to `api/index.py` and `maxDuration` 300; its
`excludeFiles` keeps tests, fixtures, tools, docs, the CLI and dev files out of the function.

## Limits that come from Vercel

- **Request and response bodies: 4.5 MB.** The app caps one upload at 4.0 MB total, and the page
  text the browser holds between triage and annotation at 3 MB.
- **Function duration: 300 s on Hobby; up to 800 s on Pro.** `vercel.json` sets 300. Triage and
  annotation are separate requests, each with its own 300 s. On Pro, set `maxDuration` to 800 and
  `CANETRUST_DEADLINE_SECONDS` to about 780.
- **Python bundle: 500 MB.** Only runtime files are bundled (see `excludeFiles`).

## Measured (local, real API, claude-sonnet-4-6, November 2026)

The fictional Marlowe packet (5 PDFs, 7 sheets): triage 12.6 s; annotation of one chain (a
restatement plus one amendment) with the facts lane beside it 138.8 s. Longer chains take
longer: a chain with four amendments may approach the 300 s limit on Hobby.

## Known limitations

- Scanned PDFs are not supported yet (OCR is milestone 9), so many real documents will be refused.
- A long chain may exceed 300 s on Vercel Hobby; there is no retry or resume across requests.
- Documents are sent to the model provider in full; there is no confidentiality routing.
