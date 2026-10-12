"""
Web API (FastAPI on Vercel's Python runtime).

    POST /api/annotate   multipart upload in, newline-delimited JSON events out
    GET  /api/health     is the deployment configured?

How a request flows
    1. Cheap checks first, answered with a normal HTTP error: access code, file count and
       size, file types, then text extraction and packet validation (service.prepare_packet).
       A scanned PDF, a duplicate amendment number or an oversized upload fails HERE, in
       well under a second, not minutes into a model call.
    2. Only then does the response start streaming. The model call runs in a worker thread
       and the response emits progress events and a heartbeat every few seconds. Vercel
       recommends this for long requests: idle HTTP/1.1 connections can be closed by
       intermediaries, and a stream also lets the page show that work is happening.
    3. The finished workbook travels in the last event, base64-encoded (it is tens of KB).
       No storage is needed, and nothing is written to disk.

Two ways to call /api/annotate
    streaming   send the header  Accept: application/x-ndjson  (the new page does). Progress
                events and a heartbeat stream back; see below.
    classic     no such header. The old contract still works: one file in the field `file`
                (or `trust`), optional `label` and `status`; the workbook bytes come back in
                the response, as the original endpoint did. Errors are JSON with a status code.
                Use this to keep an existing page working while a new one is built.

Events (one JSON object per line, streaming mode):
    {"type":"progress","message":"..."}
    {"type":"heartbeat","elapsed":42}
    {"type":"done","filename":"...","route":"single|multi","warnings":[...],"xlsx_base64":"..."}
    {"type":"error","code":"...","message":"..."}

Nothing from the uploaded documents is logged: only event types, sizes and error classes.
"""

import base64
import dataclasses
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

from extract import SUPPORTED_SUFFIXES, ExtractionError
from packet import MAX_AMENDMENTS
from service import AmendmentUpload, UploadedFile, annotate_packet, prepare_packet
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


def _parse_amendments(meta_json: str, files: list[UploadFile]) -> list[tuple[UploadFile, int, str]]:
    try:
        meta = json.loads(meta_json or "[]")
    except json.JSONDecodeError:
        raise ApiError(400, "bad_request", "amendments_meta is not valid JSON.") from None
    if not isinstance(meta, list) or len(meta) != len(files):
        raise ApiError(400, "bad_request",
                       "Send one amendments_meta entry for each amendment file, in the same order.")
    if len(files) > MAX_AMENDMENTS:
        raise ApiError(400, "too_many_amendments", f"At most {MAX_AMENDMENTS} amendments are supported.")

    parsed = []
    for upload, entry in zip(files, meta):
        number = entry.get("number") if isinstance(entry, dict) else None
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ApiError(400, "bad_amendment_number",
                           f"{upload.filename}: the amendment number must be a whole number, 1 or higher.")
        label = str(entry.get("label") or "").strip() or f"Amendment {number}"
        parsed.append((upload, number, label))
    return parsed


def _content_disposition(filename: str) -> str:
    """ASCII fallback plus the RFC 5987 form, so any characters in a name are safe in a header."""
    fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "annotation.xlsx"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}, separators=(",", ":")) + "\n"


def _describe_failure(exc: Exception) -> tuple[str, str]:
    """(code, message) safe to show a user. Never includes document text or secrets."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "model_auth", "The server could not authenticate with the model provider. Tell the administrator."
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", "The model provider is rate-limiting requests. Wait a minute and try again."
    if isinstance(exc, anthropic.APIConnectionError):          # includes timeouts
        return "model_unreachable", "Could not reach the model provider. Try again."
    if isinstance(exc, anthropic.APIStatusError):
        return "model_error", f"The model provider returned an error (HTTP {exc.status_code}). Try again."
    if isinstance(exc, ValidationError):
        return "bad_model_output", ("The model's answer did not match the expected form. "
                                    "Try again; if it keeps happening, tell the administrator.")
    if isinstance(exc, RuntimeError):                          # crafted messages in llm*.py / settings
        return "run_failed", str(exc)
    return "internal", "Something went wrong while building the workbook."


def _log_failure(exc: Exception) -> None:
    """Server-side log line. Deliberately content-free: an unexpected exception's message
    could contain text from a document, so only messages we wrote ourselves are logged."""
    if isinstance(exc, ValidationError):
        detail = str([(list(e["loc"]), e["type"]) for e in exc.errors(include_input=False)][:5])
    elif isinstance(exc, anthropic.APIStatusError):
        detail = f"HTTP {exc.status_code}"
    elif isinstance(exc, RuntimeError):                        # messages written in llm*.py / settings
        detail = str(exc)[:300]
    else:
        detail = "(message withheld)"
    print(f"[error] {type(exc).__name__}: {detail}", file=sys.stderr)


def _stream_annotation(packet, settings: Settings):
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            result = annotate_packet(packet, settings, progress=lambda m: events.put(("progress", m)))
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
                xlsx_base64=base64.b64encode(payload.xlsx).decode("ascii"),
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
            settings.template_single, settings.template_multi,
            settings.rules_single, settings.rules_multi)),
        "max_upload_mb": round(settings.max_upload_bytes / 1024 / 1024, 1),
        "supported_types": list(SUPPORTED_SUFFIXES),
    }


@app.post("/api/annotate")
def annotate(
    request: Request,
    trust: Annotated[UploadFile | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,            # classic field name for the trust
    amendment_files: Annotated[list[UploadFile] | None, File()] = None,
    amendments_meta: Annotated[str, Form()] = "[]",
    doc_name: Annotated[str, Form()] = "",
    trust_label: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",                           # classic: the trust's label
    status: Annotated[str, Form()] = "",                          # classic: the trust's status
    access_code: Annotated[str, Form()] = "",
):
    settings = load_settings()
    _check_access(settings, request.headers.get("x-access-code") or access_code)

    trust_upload = trust or file
    if trust_upload is None:
        raise ApiError(422, "missing_file", "Upload the trust file in the field 'trust' (or 'file').")
    if status.strip():
        settings = dataclasses.replace(settings, layer_status=status.strip()[:60])

    amendment_files = amendment_files or []
    parsed = _parse_amendments(amendments_meta, amendment_files)

    budget = settings.max_upload_bytes
    trust_file = _read_upload(trust_upload, budget)
    budget -= len(trust_file.data)
    uploads = []
    for upload, number, amendment_label in parsed:
        uploaded = _read_upload(upload, budget)
        budget -= len(uploaded.data)
        uploads.append(AmendmentUpload(file=uploaded, number=number, label=amendment_label))

    stem = Path(trust_file.filename).stem
    try:
        packet = prepare_packet(
            doc_name=doc_name.strip() or stem,
            trust=trust_file,
            trust_label=(trust_label or label).strip() or stem,
            amendments=uploads,
            settings=settings,
        )
    except ExtractionError as exc:
        raise ApiError(_STATUS_FOR_KIND.get(exc.kind, 400), exc.kind, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_packet", str(exc)) from exc

    if "application/x-ndjson" in (request.headers.get("accept") or ""):
        return StreamingResponse(
            _stream_annotation(packet, settings),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # Classic mode: run to completion and return the workbook itself.
    try:
        result = annotate_packet(packet, settings)
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
