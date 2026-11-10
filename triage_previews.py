"""
Page previews for triage (docs/DESIGN.md, "Triage"): the model sees a short preview of every
page, never the whole packet. A preview is

    [<file_id> sheet <n>]
    the first 2 and last 6 non-empty lines, each cut at about 160 characters
    key lines: up to 4 lines from the middle of the sheet that revoke, restate or amend
    features: ...      computed here, in code, not by the model

Key lines exist because an amendment often revokes or restates an earlier instrument in its
first numbered section, which the first and last lines miss; without them triage cannot fill
`revokes[]` (found in the first real run on the fictional Marlowe packet).

Features: the printed-number candidate (page_labels), document-system numbers in the footer, form
numbers, dates found by pattern, and a count of "University of Miami" mentions. Dates and
mentions are scanned over the whole page; the other features over the header and footer lines.

Previews contain page text. They go to the model and nowhere else: never log or print them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from page_labels import BARE_SLACK, FOOTER_LINES, HEADER_LINES, find_label_candidate
from page_reader import PageRecord

LINE_CHARS = 160
MAX_DATES = 4

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|"
           "December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+day\s+of\s+(?:{_MONTHS}),?\s+\d{{4}}\b", re.I),
]
_DOC_NUMBER_RES = [
    re.compile(r"(?<![\w#])\d{6,}(?!\w)"),          # a bare run of 6+ digits
    re.compile(r"#\d{5,}(?:v\d+)?\b", re.I),         # "#12345678v2" style
]
_FORM_RE = re.compile(r"\bForm\s+No\.?\s*[\w.\-]+", re.I)
_UM_RE = re.compile(r"University\s+of\s+Miami", re.I)
_KEY_RE = re.compile(r"\brevok\w*|\brevocation\b|\bhereby\s+(?:amend|restate)\w*|\bamended\s+and\s+restated\b"
                     r"|\bin\s+(?:its|their)\s+entirety\b", re.I)
MAX_KEY_LINES = 4


@dataclass(frozen=True)
class PageFeatures:
    printed: str | None
    doc_numbers: tuple[str, ...]
    forms: tuple[str, ...]
    dates: tuple[str, ...]
    um_mentions: int


@dataclass(frozen=True)
class Preview:
    file_id: str
    sheet: int
    text: str
    features: PageFeatures


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _unique(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def page_features(text: str, *, max_bare: int) -> PageFeatures:
    lines = _lines(text)
    header = lines[:HEADER_LINES]
    footer = lines[max(HEADER_LINES, len(lines) - FOOTER_LINES):]
    doc_numbers = _unique(m.group(0) for line in footer for r in _DOC_NUMBER_RES
                          for m in r.finditer(line))
    forms = _unique(m.group(0) for line in header + footer for m in _FORM_RE.finditer(line))
    found = sorted((m.start(), m.group(0)) for r in _DATE_RES for m in r.finditer(text))
    dates = _unique(" ".join(d.split()) for _, d in found)[:MAX_DATES]
    return PageFeatures(find_label_candidate(text, max_bare=max_bare), doc_numbers, forms, dates,
                        len(_UM_RE.findall(text)))


def _features_line(f: PageFeatures) -> str:
    parts = [f"printed={f.printed or 'none'}"]
    if f.doc_numbers:
        parts.append("doc_number=" + ", ".join(f.doc_numbers))
    if f.forms:
        parts.append("form=" + ", ".join(f.forms))
    if f.dates:
        parts.append("dates=" + "; ".join(f.dates))
    parts.append(f"um_mentions={f.um_mentions}")
    return "features: " + " | ".join(parts)


def build_preview(page: PageRecord, *, sheets_in_file: int, max_chars: int) -> Preview:
    lines = _lines(page.text)
    if len(lines) <= HEADER_LINES + FOOTER_LINES:
        picked = lines
    else:
        middle = lines[HEADER_LINES:-FOOTER_LINES]
        key = [line for line in middle if _KEY_RE.search(line)][:MAX_KEY_LINES]
        picked = lines[:HEADER_LINES] + (["key lines:"] + key + ["..."] if key else ["..."]) + lines[-FOOTER_LINES:]
    picked = [line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + "…" for line in picked]
    features = page_features(page.text, max_bare=sheets_in_file + BARE_SLACK)
    head = f"[{page.file_id} sheet {page.position}]"
    tail = _features_line(features)
    body = "\n".join(picked) if picked else "(no text on this sheet)"
    room = max(0, max_chars - len(head) - len(tail) - 2)
    if len(body) > room:
        body = body[:room]
    return Preview(page.file_id, page.position, f"{head}\n{body}\n{tail}", features)


def build_previews(pages_by_file: dict[str, list[PageRecord]], *, max_chars: int) -> list[Preview]:
    """Every page's preview, files in the order given, sheets in order."""
    return [build_preview(page, sheets_in_file=len(pages), max_chars=max_chars)
            for pages in pages_by_file.values() for page in pages]


def estimate_tokens(previews) -> int:
    """A rough count (about 4 characters per token), without a network call."""
    return sum(len(p.text if isinstance(p, Preview) else p) for p in previews) // 4 + 1
