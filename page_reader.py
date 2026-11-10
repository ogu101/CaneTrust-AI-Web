"""
Page reader: file bytes in, one PageRecord per physical page out (docs/DESIGN.md, "Page reader
boundary"). Today the text comes from the PDF text layer; a later OCR step fills the same
records, so nothing downstream changes when it arrives.

The reader records each page's label *candidate* only. Labels are assigned per document by
page_labels.label_document(), after triage has found where each document starts and ends,
because printed numbering restarts in every document. A page's position in its file is kept
for ordering and is never used as its label.

Refusals: unsupported type, empty, invalid, encrypted, too many pages, and scans (no text
layer), each an ExtractionError whose `kind` the web API maps to an HTTP status. OCR is not done
here: a scan is refused with an explanation (docs/DESIGN.md, milestone 9).
"""

import io
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader

from page_labels import BARE_SLACK, find_label_candidate

SUPPORTED_SUFFIXES = (".pdf", ".txt")

# A page with fewer characters than this is treated as having no text layer. A real page of a
# trust is hundreds of characters; a scan extracts as nothing, or a few stray characters.
MIN_PAGE_CHARS = 40
# A PDF is accepted if at least this share of its pages has text. Below it, it is a scan.
MIN_TEXT_PAGE_SHARE = 0.5


class ExtractionError(ValueError):
    """A file we can't turn into pages. `kind` says why, so callers can choose a status code:
    unsupported | empty | invalid | encrypted | too_long | no_text_layer."""

    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def _suffix(filename: str) -> str:
    name = filename.lower()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def unreadable_sheets(pages: list["PageRecord"]) -> list[int]:
    """Sheets with no usable text (possibly scanned images inside a text PDF)."""
    return [p.position for p in pages if len(p.text) < MIN_PAGE_CHARS]


def describe_unreadable(filename: str, sheets: list[int], limit: int = 8) -> str | None:
    """A warning naming the sheets of a file that have no extractable text, or None."""
    if not sheets:
        return None
    shown = ", ".join(str(n) for n in sheets[:limit])
    more = f" and {len(sheets) - limit} more" if len(sheets) > limit else ""
    return (f"{filename}: sheet(s) {shown}{more} have no extractable text (possibly scanned "
            f"images). Anything on them could not be read.")

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
