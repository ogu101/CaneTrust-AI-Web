"""
Page source: where page text comes from (docs/DESIGN.md, "Page source"). Everything downstream
takes PageRecords from load_pages() and never asks how the text was obtained.

Order:
  1. The PDF text layer (or a .txt file), via page_reader.read_pages().
  2. Tests and dev tools only: a sidecar file <sidecar_dir>/<filename>.pages.json, read only when
     the file has no text layer and the caller passes sidecar_dir. The web API never passes it,
     so it never reads sidecars.
  3. Otherwise the original no_text_layer ExtractionError.

The sidecar format is the plug-in point for the OCR step: whatever OCR produces must be a JSON
list of page texts in sheet order, exactly one string per sheet of the PDF ("" for a blank
sheet). Errors name the file and counts only, never page text.
"""

import io
import json
from pathlib import Path

from pypdf import PdfReader

from extract import ExtractionError
from page_reader import PageRecord, read_pages, records_from_texts

SIDECAR_SUFFIX = ".pages.json"


def sidecar_path(sidecar_dir: Path, filename: str) -> Path:
    return Path(sidecar_dir) / f"{Path(filename).name}{SIDECAR_SUFFIX}"


def load_pages(file_id: str, filename: str, data: bytes, *, sidecar_dir: Path | None = None,
               max_pages: int = 250) -> list[PageRecord]:
    try:
        return read_pages(file_id, filename, data, max_pages=max_pages)
    except ExtractionError as exc:
        if exc.kind != "no_text_layer" or sidecar_dir is None:
            raise
        path = sidecar_path(sidecar_dir, filename)
        if not path.is_file():
            raise
    return records_from_texts(file_id, _read_sidecar(path, filename, _sheet_count(data)))


def _sheet_count(data: bytes) -> int:
    # read_pages has already opened this PDF and found it a scan, so it parses.
    return len(PdfReader(io.BytesIO(data), strict=False).pages)


def _read_sidecar(path: Path, filename: str, sheets: int) -> list[str]:
    try:
        texts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"{path.name}: could not read sidecar ({type(exc).__name__})",
                              kind="invalid") from exc
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise ExtractionError(f"{path.name}: sidecar must be a JSON list of page texts",
                              kind="invalid")
    if len(texts) != sheets:
        raise ExtractionError(
            f"{path.name}: sidecar has {len(texts)} page texts but {filename} has {sheets} sheets",
            kind="invalid")
    return [t.strip() for t in texts]
