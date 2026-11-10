"""
Page reader: file bytes in, one PageRecord per physical page out (docs/DESIGN.md, "Page reader
boundary"). Today the text comes from the PDF text layer; a later OCR step fills the same
records, so nothing downstream changes when it arrives.

The reader records each page's label *candidate* only. Labels are assigned per document by
page_labels.label_document(), after triage has found where each document starts and ends,
because printed numbering restarts in every document. A page's position in its file is kept
for ordering and is never used as its label.

Refusals match extract.py: unsupported type, empty, invalid, encrypted, too many pages, and
scans (no text layer), each an ExtractionError with the same `kind`.

TODO(milestone 6): the PDF opening and scan checks below repeat extract._extract_pdf. The
duplication goes away when the old pipeline (extract.py and its [PDF page N of M] markers) is
removed.
"""

import io
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader

from extract import (
    MIN_PAGE_CHARS, MIN_TEXT_PAGE_SHARE, SUPPORTED_SUFFIXES, ExtractionError, _decode_text, _suffix,
)
from page_labels import BARE_SLACK, find_label_candidate

LabelSource = Literal["printed", "inferred", "unnumbered"]


@dataclass(frozen=True)
class PageRecord:
    file_id: str
    position: int                           # 1-based sheet in the file; ordering only, never a label
    text: str                               # "" when the page has no extractable text
    label_candidate: str | None = None      # raw printed number found on the page, e.g. "Page 7 of 41"
    label: str | None = None                # set by page_labels.label_document()
    label_source: LabelSource | None = None


def read_pages(file_id: str, filename: str, data: bytes, *, max_pages: int = 250) -> list[PageRecord]:
    """A text-based PDF gives one record per page; a .txt file splits on form feeds."""
    suffix = _suffix(filename)
    if suffix not in SUPPORTED_SUFFIXES:
        raise ExtractionError(
            f"{filename}: unsupported file type {suffix or '(none)'!r}. Upload a "
            f"text-based PDF or a .txt file.",
            kind="unsupported",
        )
    if not data or not data.strip():
        raise ExtractionError(f"{filename}: file is empty", kind="empty")

    if suffix == ".txt":
        texts = [part.strip() for part in _decode_text(data).strip().split("\f")]
        if not any(texts):
            raise ExtractionError(f"{filename}: file is empty", kind="empty")
        if len(texts) > max_pages:
            raise ExtractionError(
                f"{filename}: {len(texts)} pages is more than the {max_pages}-page limit",
                kind="too_long")
    else:
        texts = _pdf_page_texts(filename, data, max_pages=max_pages)

    return records_from_texts(file_id, texts)


def records_from_texts(file_id: str, texts: list[str]) -> list[PageRecord]:
    """One record per page text, in sheet order, with its label candidate. Shared by every page
    source (text layer, .txt, sidecar, and later OCR) so they all fill records the same way."""
    max_bare = len(texts) + BARE_SLACK
    return [
        PageRecord(file_id, position, text, find_label_candidate(text, max_bare=max_bare))
        for position, text in enumerate(texts, start=1)
    ]


def _pdf_page_texts(filename: str, data: bytes, *, max_pages: int) -> list[str]:
    if not data.lstrip().startswith(b"%PDF"):
        raise ExtractionError(f"{filename}: this is not a valid PDF", kind="invalid")

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError(
                f"{filename}: this PDF is password-protected. Remove the password and "
                f"upload it again.", kind="encrypted")
        page_count = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:                      # pypdf raises many types on bad input
        raise ExtractionError(f"{filename}: could not read this PDF ({type(exc).__name__})",
                              kind="invalid") from exc

    if page_count == 0:
        raise ExtractionError(f"{filename}: this PDF has no pages", kind="empty")
    if page_count > max_pages:
        raise ExtractionError(
            f"{filename}: {page_count} pages is more than the {max_pages}-page limit",
            kind="too_long")

    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:                          # one bad page must not sink the file
            texts.append("")

    readable = sum(1 for text in texts if len(text) >= MIN_PAGE_CHARS)
    if not readable or readable / page_count < MIN_TEXT_PAGE_SHARE:
        raise ExtractionError(
            f"{filename}: this PDF has no text layer (it looks like a scan). Scanned "
            f"documents are not supported yet. Upload a text-based PDF, or a .txt "
            f"version of the document.",
            kind="no_text_layer")
    return texts
