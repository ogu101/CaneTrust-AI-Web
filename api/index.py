"""
Web API (FastAPI on Vercel's Python runtime).

    POST /api/triage     files in (multipart) -> NDJSON progress, then the plan and page texts
    POST /api/annotate   confirmed plan and page texts in (JSON) -> NDJSON progress, then the workbook
                         or the legacy request (multipart: one file, label, status) -> the workbook
    GET  /api/health     is the deployment configured?

Two requests, never one held open while a person decides (docs/DESIGN.md, "API and hosting").
Between them the browser holds the state: the plan and the page texts, capped at
CANETRUST_MAX_STATE_MB (default 3 MB of page text). The server trusts none of it: /api/annotate
re-validates the plan (coverage, ranges, types, treatments, overrides), rebuilds the page records,
and recomputes labels and chains before any model call.

How a request flows
    1. Cheap checks first, answered with a normal HTTP error: access code, size, file types, page
       text (a scan is refused here), the state cap, the plan. Nothing streams until they pass.
    2. Then the response streams. The slow work runs in a worker thread; the stream carries its
       progress and a heartbeat every 10 s (idle connections can be closed by intermediaries).
    3. The result travels in the last event: the plan and page texts after triage, the workbook
       (base64) after annotation. No storage is needed, and nothing is written to disk.

Events (one JSON object per line):
    {"type":"progress","message":"..."}
    {"type":"heartbeat","elapsed":42}
    {"type":"triaged","plan":{...},"pages":{"file id":["page text", ...]},"skip_confirm":false,"warnings":[...]}
    {"type":"done","filename":"...","route":"unified","warnings":[...],"chains":[...],"xlsx_base64":"..."}
    {"type":"error","code":"...","message":"..."}

The legacy request (multipart to /api/annotate: `file` or `trust`, optional `label`, `status`,
`amendment_files`) runs the one-shot path: triage, accept triage's plan as it stands, annotate.
Without `Accept: application/x-ndjson` it answers with the workbook bytes, as the first version
did; if triage needs a person it answers 409 needs_confirmation (use the two-step flow).

Nothing from the uploaded documents is logged: only event types, sizes and error classes.
"""

import base64
import hmac
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from page_reader import SUPPORTED_SUFFIXES, ExtractionError
from plan import validate_plan
from service import (
    UploadedFile, annotate_one_shot, annotate_plan, can_skip_confirm, confirmed_plan_problems,
    describe_failure, load_files, page_texts, pages_from_texts, state_bytes, triage_packet,
)
from settings import Settings, load_local_env, load_settings

load_local_env()

app = FastAPI(title="CaneTrust", docs_url=None, redoc_url=None, openapi_url=None)

HEARTBEAT_SECONDS = 10
MAX_FILES = 40

_STATUS_FOR_KIND = {
    "unsupported": 415,
    "too_long": 413,
    "no_text_layer": 422,
    "encrypted": 422,
    "empty": 400,
    "invalid": 400,
}

_STATUS_FOR_FAILURE = {
    "needs_confirmation": 409,
    "rate_limited": 429,
    "model_auth": 502,
    "model_unreachable": 502,
    "model_error": 502,
    "bad_model_output": 502,
    "run_failed": 500,
    "internal": 500,
}

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NDJSON_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError):
    return JSONResponse({"error": exc.message, "code": exc.code, **exc.extra}, status_code=exc.status,
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _check_access(settings: Settings, supplied: str) -> None:
    """A public URL that spends money on every call needs a gate. On Vercel a deployment
    with no access code configured refuses to run rather than running open."""
    if settings.access_code is None:
        if os.getenv("VERCEL") and not settings.allow_open:
            raise ApiError(503, "not_configured",
                           "This deployment has no access code set. The administrator "
                           "must set CANETRUST_ACCESS_CODE.")
        return
    if not hmac.compare_digest((supplied or "").encode(), settings.access_code.encode()):
        raise ApiError(401, "bad_access_code", "That access code is not correct.")


def _too_large_message(limit_bytes: int) -> str:
    return (f"The files together are over the {limit_bytes / 1024 / 1024:.1f} MB limit for one "
            f"upload. Send fewer or smaller files.")


async def _read_uploads(uploads: list, settings: Settings) -> list[UploadedFile]:
    if not uploads:
        raise ApiError(422, "missing_file", "Upload at least one file.")
    if len(uploads) > MAX_FILES:
        raise ApiError(400, "too_many_files", f"Send at most {MAX_FILES} files at a time.")
    budget, out = settings.max_upload_bytes, []
    for upload in uploads:
        data = await upload.read(budget + 1)
        if len(data) > budget:
            raise ApiError(413, "too_large", _too_large_message(settings.max_upload_bytes))
        budget -= len(data)
        out.append(UploadedFile(filename=upload.filename or "upload", data=data))
    return out


def _load(files: list[UploadedFile], settings: Settings):
    try:
        return load_files(files, settings)
    except ExtractionError as exc:
        raise ApiError(_STATUS_FOR_KIND.get(exc.kind, 400), exc.kind, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_packet", str(exc)) from exc


def _state_too_large(size: int, settings: Settings) -> ApiError:
    return ApiError(413, "state_too_large",
                    f"These files hold {size / 1048576:.1f} MB of page text, over the "
                    f"{settings.max_state_bytes / 1048576:.1f} MB the browser can keep between the two "
                    f"steps. Send fewer files at a time.")


def _content_disposition(filename: str) -> str:
    """ASCII fallback plus the RFC 5987 form, so any characters in a name are safe in a header."""
    fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "annotation.xlsx"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}, separators=(",", ":")) + "\n"


_describe_failure = describe_failure


def _log_failure(exc: Exception) -> None:
    """Server-side log line. Deliberately content-free: an unexpected exception's message
    could contain text from a document, so only messages we wrote ourselves are logged."""
    if isinstance(exc, ValidationError):
        detail = str([(list(e["loc"]), e["type"]) for e in exc.errors(include_input=False)][:5])
    elif isinstance(exc, anthropic.APIStatusError):
        detail = f"HTTP {exc.status_code}"
    elif isinstance(exc, RuntimeError):                        # messages we wrote (annotate, triage, settings)
        detail = str(exc)[:300]
    else:
        detail = "(message withheld)"
    print(f"[error] {type(exc).__name__}: {detail}", file=sys.stderr)


def _stream(work, settings: Settings, first_message: str):
    """Run work(progress) in a thread; stream its progress, heartbeats, and a final event made
    by work's return value: (event type, fields)."""
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            events.put(("result", work(lambda m: events.put(("progress", m)))))
        except Exception as exc:                              # reported to the client, not raised
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    started = time.monotonic()
    yield _event("progress", message=first_message)
    while True:
        if time.monotonic() - started > settings.deadline_seconds:
            yield _event("error", code="deadline",
                         message=(f"This is taking longer than the {settings.deadline_seconds} seconds "
                                  f"the server allows. Try fewer or shorter documents."))
            return
        try:
            kind, payload = events.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            yield _event("heartbeat", elapsed=int(time.monotonic() - started))
            continue
        if kind == "progress":
            yield _event("progress", message=payload)
        elif kind == "result":
            event_type, fields = payload
            yield _event(event_type, **fields)
            return
        else:
            _log_failure(payload)
            code, message = _describe_failure(payload)
            yield _event("error", code=code, message=message)
            return


def _done_fields(result) -> dict:
    return {"filename": result.filename, "route": result.route, "warnings": result.warnings,
            "chains": result.chains, "xlsx_base64": base64.b64encode(result.xlsx).decode("ascii")}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    settings = load_settings()
    return {
        "ok": True,
        "model": settings.model,
        "api_key_set": bool(settings.api_key),
        "access_code_required": settings.access_code is not None,
        "open_access_allowed": settings.allow_open,
        "assets_present": all(p.is_file() for p in (
            settings.template, settings.annotation_rules, settings.triage_rules,
            settings.default_treatments)),
        "max_upload_mb": round(settings.max_upload_bytes / 1024 / 1024, 1),
        "max_state_mb": round(settings.max_state_bytes / 1024 / 1024, 1),
        "supported_types": list(SUPPORTED_SUFFIXES),
    }


@app.post("/api/triage")
async def triage_route(request: Request):
    settings = load_settings()
    form = await request.form()
    _check_access(settings, request.headers.get("x-access-code") or str(form.get("access_code") or ""))
    uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    files = await _read_uploads(uploads, settings)
    loaded = await run_in_threadpool(_load, files, settings)
    texts = page_texts(loaded)
    size = state_bytes(texts)
    if size > settings.max_state_bytes:
        raise _state_too_large(size, settings)
    name = str(form.get("doc_name") or "").strip() or None

    def work(progress):
        outcome = triage_packet(loaded, settings, progress, name=name)
        return "triaged", {"plan": outcome.plan.to_dict(), "pages": texts,
                           "skip_confirm": can_skip_confirm(outcome.plan), "warnings": outcome.warnings}

    return StreamingResponse(_stream(work, settings, "Files accepted."),
                             media_type="application/x-ndjson", headers=NDJSON_HEADERS)


@app.post("/api/annotate")
async def annotate_route(request: Request):
    settings = load_settings()
    if (request.headers.get("content-type") or "").startswith("application/json"):
        return await _annotate_plan(request, settings)
    return await _annotate_legacy(request, settings)


async def _annotate_plan(request: Request, settings: Settings):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "The request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Send a JSON object with 'plan' and 'pages'.")
    _check_access(settings, request.headers.get("x-access-code") or str(body.get("access_code") or ""))
    texts = body.get("pages")
    if isinstance(texts, dict):
        size = sum(len(t.encode("utf-8")) for v in texts.values() if isinstance(v, list)
                   for t in v if isinstance(t, str))
        if size > settings.max_state_bytes:
            raise _state_too_large(size, settings)
    try:
        pages_by_file = pages_from_texts(texts, settings)
    except ValueError as exc:
        raise ApiError(400, "bad_pages", str(exc)) from exc
    plan, problems = validate_plan(body.get("plan"), {f: len(p) for f, p in pages_by_file.items()})
    if not problems:
        problems = confirmed_plan_problems(plan)
    if problems:
        raise ApiError(422, "invalid_plan", "The document map has problems: " + "; ".join(problems[:5]),
                       problems=problems)

    def work(progress):
        return "done", _done_fields(annotate_plan(plan, pages_by_file, settings, progress))

    return StreamingResponse(_stream(work, settings, "Plan accepted."),
                             media_type="application/x-ndjson", headers=NDJSON_HEADERS)


async def _annotate_legacy(request: Request, settings: Settings):
    """The original contract: `file` (or `trust`) plus optional `amendment_files`, `label`,
    `status` (accepted; status now comes from the chain), `doc_name`, `trust_label`."""
    form = await request.form()
    _check_access(settings, request.headers.get("x-access-code") or str(form.get("access_code") or ""))
    first = form.get("trust") or form.get("file")
    if not isinstance(first, UploadFile):
        raise ApiError(422, "missing_file", "Upload the trust file in the field 'trust' (or 'file').")
    extra = [f for f in form.getlist("amendment_files") if isinstance(f, UploadFile)]
    files = await _read_uploads([first, *extra], settings)
    await run_in_threadpool(_load, files, settings)             # fast checks before anything streams
    name = next((str(form.get(k)).strip() for k in ("doc_name", "trust_label", "label")
                 if str(form.get(k) or "").strip()), None) or Path(files[0].filename).stem

    if "application/x-ndjson" in (request.headers.get("accept") or ""):
        def work(progress):
            return "done", _done_fields(annotate_one_shot(files, settings, progress, name=name))
        return StreamingResponse(_stream(work, settings, "Documents accepted."),
                                 media_type="application/x-ndjson", headers=NDJSON_HEADERS)

    try:
        result = await run_in_threadpool(annotate_one_shot, files, settings, name=name)
    except Exception as exc:
        _log_failure(exc)
        code, message = _describe_failure(exc)
        raise ApiError(_STATUS_FOR_FAILURE.get(code, 500), code, message) from exc
    return Response(
        content=result.xlsx,
        media_type=XLSX,
        headers={
            "Content-Disposition": _content_disposition(result.filename),
            "X-CaneTrust-Route": result.route,
            "Cache-Control": "no-store",
        },
    )


# Locally (uvicorn), serve the page too. On Vercel, public/ is served by the CDN.
if not os.getenv("VERCEL"):
    @app.get("/", include_in_schema=False)
    def local_index():
        return FileResponse(ROOT / "public" / "index.html")
