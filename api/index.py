"""
Web API (FastAPI on Vercel's Python runtime).

    POST /api/annotate   multipart upload in, newline-delimited JSON events out
    GET  /api/health     is the deployment configured?

How a request flows
    1. Cheap checks first, answered with a normal HTTP error: access code, file count and
       size, file types, then page text and size limits (service.load_files). A scanned PDF
       or an oversized upload fails HERE, in well under a second, not minutes into a model call.
    2. Only then does the response start streaming. The pipeline (triage, then every chain
       annotated with the facts lane beside it; service.annotate_one_shot) runs in a worker thread
       and the response emits progress events and a heartbeat every few seconds. Vercel
       recommends this for long requests: idle HTTP/1.1 connections can be closed by
       intermediaries, and a stream also lets the page show that work is happening.
    3. The finished workbook travels in the last event, base64-encoded (it is tens of KB).
       No storage is needed, and nothing is written to disk.

Two ways to call /api/annotate
    streaming   send the header  Accept: application/x-ndjson  (the new page does). Progress
                events and a heartbeat stream back; see below.
    classic     no such header. The old contract still works: one file in the field `file`
                (or `trust`), optional `label` (the matter name) and `status` (accepted; the
                status now comes from the chain); the workbook bytes come back in the response.
                Errors are JSON with a status code. If triage needs a person's decision the
                answer is 409 needs_confirmation: use the confirm screen.
                Use this to keep an existing page working while a new one is built.

Events (one JSON object per line, streaming mode):
    {"type":"progress","message":"..."}
    {"type":"heartbeat","elapsed":42}
    {"type":"done","filename":"...","route":"unified","warnings":[...],"chains":[...],"xlsx_base64":"..."}
    {"type":"error","code":"...","message":"..."}

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
from typing import Annotated
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anthropic
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from page_reader import SUPPORTED_SUFFIXES, ExtractionError
from service import NeedsConfirmation, UploadedFile, annotate_one_shot, describe_failure, load_files
from settings import Settings, load_local_env, load_settings

load_local_env()

app = FastAPI(title="CaneTrust", docs_url=None, redoc_url=None, openapi_url=None)

HEARTBEAT_SECONDS = 10

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


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError):
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status,
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
    if not hmac.compare_digest(supplied.encode(), settings.access_code.encode()):
        raise ApiError(401, "bad_access_code", "That access code is not correct.")


def _read_upload(upload: UploadFile, budget: int) -> UploadedFile:
    data = upload.file.read(budget + 1)
    if len(data) > budget:
        raise ApiError(413, "too_large", _too_large_message(budget))
    return UploadedFile(filename=upload.filename or "upload", data=data)


def _too_large_message(limit_bytes: int) -> str:
    return (f"The files together are over the {limit_bytes / 1024 / 1024:.1f} MB limit for one "
            f"upload. Send fewer or smaller files.")


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


def _stream_annotation(files, settings: Settings, name: str):
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            result = annotate_one_shot(files, settings, progress=lambda m: events.put(("progress", m)),
                                       name=name)
            events.put(("done", result))
        except Exception as exc:                              # reported to the client, not raised
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    started = time.monotonic()

    yield _event("progress", message="Documents accepted.")
    while True:
        elapsed = time.monotonic() - started
        if elapsed > settings.deadline_seconds:
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
        elif kind == "done":
            yield _event(
                "done", filename=payload.filename, route=payload.route, warnings=payload.warnings,
                chains=payload.chains, xlsx_base64=base64.b64encode(payload.xlsx).decode("ascii"),
            )
            return
        else:
            _log_failure(payload)
            code, message = _describe_failure(payload)
            yield _event("error", code=code, message=message)
            return


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
        "supported_types": list(SUPPORTED_SUFFIXES),
    }


@app.post("/api/annotate")
def annotate(
    request: Request,
    trust: Annotated[UploadFile | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,            # classic field name for the trust
    amendment_files: Annotated[list[UploadFile] | None, File()] = None,
    amendments_meta: Annotated[str, Form()] = "[]",               # accepted and ignored: triage reads content
    doc_name: Annotated[str, Form()] = "",
    trust_label: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",                           # classic: becomes the matter name
    status: Annotated[str, Form()] = "",                          # classic: accepted; status now comes from the chain
    access_code: Annotated[str, Form()] = "",
):
    settings = load_settings()
    _check_access(settings, request.headers.get("x-access-code") or access_code)

    first = trust or file
    if first is None:
        raise ApiError(422, "missing_file", "Upload the trust file in the field 'trust' (or 'file').")

    budget = settings.max_upload_bytes
    uploads = []
    for upload in [first, *(amendment_files or [])]:
        uploaded = _read_upload(upload, budget)
        budget -= len(uploaded.data)
        uploads.append(uploaded)

    try:
        load_files(uploads, settings)                 # fast checks before anything streams
    except ExtractionError as exc:
        raise ApiError(_STATUS_FOR_KIND.get(exc.kind, 400), exc.kind, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_packet", str(exc)) from exc
    name = (doc_name or trust_label or label).strip() or Path(uploads[0].filename).stem

    if "application/x-ndjson" in (request.headers.get("accept") or ""):
        return StreamingResponse(
            _stream_annotation(uploads, settings, name),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # Classic mode: run to completion and return the workbook itself.
    try:
        result = annotate_one_shot(uploads, settings, name=name)
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
