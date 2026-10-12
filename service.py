"""
The pipeline, in memory from end to end. Both entry points call it:

    cli.py        reads paths from config.py             -> build_packet() -> annotate_packet()
    api/index.py  receives uploads over HTTP             -> prepare_packet() -> annotate_packet()

Nothing here reads or writes the working directory. Uploads arrive as bytes and the finished
workbook leaves as bytes, which is what a serverless function needs (no durable disk) and
why the two entry points can't drift apart.

Two stages, split on purpose:
    prepare_packet   fast, no network: extract text, validate, enforce size limits.
                     Its errors are the user's to fix (bad file, scan, wrong numbering).
    annotate_packet  slow: the model call and the workbook. Its errors are the system's.
The web app runs the first before it starts streaming, so a bad upload gets an immediate,
proper HTTP error instead of a failure minutes later.
"""

import io
import re
from dataclasses import dataclass, field
from typing import Callable

from extract import extract_text
from llm import InstrumentLayer, analyze_document
from llm_multi import analyze_packet
from packet import Packet, make_packet, triage
from render import render_to_workbook
from render_multi import render_packet_to_workbook
from settings import Settings, require_api_key

Progress = Callable[[str], None]


def _quiet(_message: str) -> None:
    pass


@dataclass
class UploadedFile:
    filename: str
    data: bytes


@dataclass
class AmendmentUpload:
    file: UploadedFile
    number: int
    label: str


@dataclass
class AnnotationResult:
    xlsx: bytes
    filename: str
    route: str                                   # "single" | "multi"
    warnings: list[str] = field(default_factory=list)


def prepare_packet(
    *,
    doc_name: str,
    trust: UploadedFile,
    trust_label: str,
    amendments: list[AmendmentUpload],
    settings: Settings,
) -> Packet:
    """Uploads -> validated Packet. Raises ExtractionError / ValueError for anything the
    user can fix (unsupported type, a scan, duplicate amendment numbers, too much text)."""
    warnings: list[str] = []

    extracted_trust = extract_text(trust.filename, trust.data, max_pages=settings.max_pages)
    warnings += extracted_trust.warnings

    items = []
    for upload in amendments:
        extracted = extract_text(upload.file.filename, upload.file.data, max_pages=settings.max_pages)
        warnings += extracted.warnings
        items.append({"number": upload.number, "label": upload.label, "text": extracted.text})

    return make_packet(
        doc_name, trust_label, extracted_trust.text, items,
        warnings=warnings, max_chars=settings.max_chars,
    )


def output_filename(name: str, route: str) -> str:
    base = re.sub(r"[^\w\-. ()]+", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    base = (base or "annotation")[:80]
    return f"{base}_annotation{'_with_amendments' if route == 'multi' else ''}.xlsx"


def annotate_packet(packet: Packet, settings: Settings, progress: Progress = _quiet) -> AnnotationResult:
    """Triage, run the matching pipeline, and return the workbook as bytes."""
    api_key = require_api_key(settings)
    route = triage(packet)
    n = len(packet.amendments)
    progress(
        "No amendments: using the single-document pipeline." if route == "single"
        else f"{n} amendment(s): using the multi-document pipeline."
    )
    if packet.missing_numbers:
        progress(f"Amendment(s) {packet.missing_numbers} were not supplied; "
                 f"affected final answers will be marked provisional.")

    buffer = io.BytesIO()
    progress("Asking the model to read the documents. This can take a few minutes.")

    if route == "single":
        layer = InstrumentLayer(
            label=packet.trust.label, status=settings.layer_status, text=packet.trust.text)
        annotation = analyze_document(
            packet.name, [layer],
            api_key=api_key, model=settings.model, effort=settings.effort,
            max_tokens=settings.max_tokens_single, rules_path=settings.rules_single,
            debug_dir=settings.debug_dir,
        )
        progress("Filling in the workbook.")
        render_to_workbook(annotation, str(settings.template_single), buffer)
    else:
        annotation = analyze_packet(
            packet,
            api_key=api_key, model=settings.model, effort=settings.effort,
            max_tokens=settings.max_tokens_multi, rules_path=settings.rules_multi,
            debug_dir=settings.debug_dir,
        )
        progress("Filling in the workbook.")
        render_packet_to_workbook(annotation, packet, str(settings.template_multi), buffer)

    return AnnotationResult(
        xlsx=buffer.getvalue(),
        filename=output_filename(packet.name, route),
        route=route,
        warnings=list(packet.warnings),
    )
