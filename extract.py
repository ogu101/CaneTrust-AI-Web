"""
Text extraction: uploaded bytes in, labelled text out. No files, no working directory.

This is the step that used to be a separate hand-run script. It is now part of the
pipeline, so the command-line tool and the web app both start from raw uploads.

What it does that the old script did not:
  * works on bytes, so it runs inside a serverless function (no disk);
  * refuses a PDF with no text layer (a scan) with a clear message, instead of handing
    the model near-gibberish and getting a confident wrong answer;
  * notes individual pages that have no text, in the text itself, so the model reports
    "Could not determine" for anything that might be on them;
  * puts a marker at the top of every page, "[PDF page 3 of 12]", so citations can name
    a page that exists even when the page's own printed number differs (a cover page,
    roman-numeral front matter) or was lost in extraction.

OCR for scanned PDFs is NOT done here. A scan is rejected with an explanation.
"""

import io
from dataclasses import dataclass, field

from pypdf import PdfReader

SUPPORTED_SUFFIXES = (".pdf", ".txt")

# A page with fewer characters than this is treated as having no text layer. A real
# page of a trust is hundreds of characters; a scan extracts as nothing, or a few stray
# characters of OCR-less metadata.
MIN_PAGE_CHARS = 40
# A PDF is accepted if at least this share of its pages has text. Below it, it is a scan.
MIN_TEXT_PAGE_SHARE = 0.5


class ExtractionError(ValueError):
    """A file we can't turn into text. `kind` says why, so callers can choose a status
    code: unsupported | empty | invalid | encrypted | too_long | no_text_layer."""

    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


@dataclass
class ExtractedText:
    filename: str
    text: str
    pages: int                      # 0 for plain text
    method: str                     # "pdf-text-layer" or "plain-text"
    warnings: list[str] = field(default_factory=list)


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


def _page_list(numbers: list[int], limit: int = 8) -> str:
    shown = ", ".join(str(n) for n in numbers[:limit])
    return shown + (f" and {len(numbers) - limit} more" if len(numbers) > limit else "")


def extract_text(
    filename: str,
    data: bytes,
    *,
    max_pages: int = 250,
    page_markers: bool = True,
) -> ExtractedText:
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
        text = _decode_text(data).strip()
        if not text:
            raise ExtractionError(f"{filename}: file is empty", kind="empty")
        return ExtractedText(filename, text, pages=0, method="plain-text")

    return _extract_pdf(filename, data, max_pages=max_pages, page_markers=page_markers)


def _extract_pdf(filename: str, data: bytes, *, max_pages: int, page_markers: bool) -> ExtractedText:
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

    page_texts: list[str] = []
    for page in reader.pages:
        try:
            page_texts.append((page.extract_text() or "").strip())
        except Exception:                          # one bad page must not sink the file
            page_texts.append("")

    readable = [i for i, t in enumerate(page_texts, start=1) if len(t) >= MIN_PAGE_CHARS]
    unreadable = [i for i in range(1, page_count + 1) if i not in set(readable)]

    if not readable or len(readable) / page_count < MIN_TEXT_PAGE_SHARE:
        raise ExtractionError(
            f"{filename}: this PDF has no text layer (it looks like a scan). Scanned "
            f"documents are not supported yet. Upload a text-based PDF, or a .txt "
            f"version of the document.",
            kind="no_text_layer")

    warnings: list[str] = []
    if unreadable:
        warnings.append(
            f"{filename}: PDF page(s) {_page_list(unreadable)} have no extractable text "
            f"(possibly scanned images). Anything on them could not be read.")

    parts: list[str] = []
    for number, text in enumerate(page_texts, start=1):
        if page_markers:
            if number in unreadable:
                parts.append(f"[PDF page {number} of {page_count}: no extractable text]")
                continue
            parts.append(f"[PDF page {number} of {page_count}]\n{text}")
        elif text:
            parts.append(text)

    return ExtractedText(filename, "\n\n".join(parts), pages=page_count,
                         method="pdf-text-layer", warnings=warnings)
