"""
Printed page labels for ONE document (docs/DESIGN.md, "Printed page labels").

Citations name the number printed on the paper, never a PDF position, so this module finds
printed numbers and turns them into labels. It runs per document, after triage has found where
each document starts and ends, because numbering restarts in every document.

Where candidates come from
  * Only the first 2 and last 6 non-empty lines of a page: headers, and the footer block, where
    the printed number often sits above two or three lines of firm name and document-system ID.
  * Strong patterns count on their own:
        "Page 7", "Page 7 of 41", "Page iv"    at the end of a line ("... Trust - Page 2")
        "- 7 -", "- iv -"                      alone on a line
        "7 of 41"                              alone on a line
        "Form No. X-1.2.3- 1 of 2"             form style, at the end of a line
  * Bare numbers ("7", "vii" alone on a line) are weak: they count only when at least two
    pages agree. A bare number of 5+ digits, or larger than the page count + 10, is never a
    page number (document IDs, years). A bare number directly under a dotted-leader line is a
    contents entry, not a page number.
  * Form-style "N of M" counts only when every page carrying one has the same M and N climbs
    by one.

How labels are assigned
  * Within a document the arabic sequence climbs by one. Each candidate votes for an offset
    (printed number minus the page's index); the best-supported offset wins. Candidates that
    agree are "printed"; every other page in the run is "inferred" from the offset, which
    fills gaps, infers an unprinted page 1 when page 2 prints "2", and covers trailing pages.
  * Pages before the arabic run's page 1 are front matter. Roman numerals there get their own
    sequence, chosen the same way. Front-matter pages without one, such as a cover page, are
    "unnumbered".
  * A document with no usable printed numbers is labelled "unnumbered, sheet N of M".

Warnings name sheets, file positions, numbers and rule names only, never document text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from page_reader import PageRecord

HEADER_LINES = 2
FOOTER_LINES = 6
MAX_BARE_DIGITS = 4          # a bare number of 5+ digits is a document ID, never a page
BARE_SLACK = 10              # a bare number above page count + 10 is not a page number
MAX_ROMAN = 50

_NUM = r"\d{1,4}|[ivxl]{1,7}|[IVXL]{1,7}"
_OF = r"(?:of|OF|Of)"
_PAGE_RE = re.compile(
    rf"(?:^|(?<=[\s\-–—|,(]))(?P<raw>(?:Page|PAGE)\s+(?P<n>{_NUM})(?:\s+{_OF}\s+(?P<m>\d{{1,4}}))?)\s*$")
_DASHED_RE = re.compile(rf"^[-–—]\s*(?P<n>{_NUM})\s*[-–—]$")
_N_OF_M_RE = re.compile(rf"^(?P<n>\d{{1,4}})\s+{_OF}\s+(?P<m>\d{{1,4}})$")
_FORM_RE = re.compile(rf"(?<=\s)(?P<raw>(?P<n>\d{{1,4}})\s+{_OF}\s+(?P<m>\d{{1,4}}))\s*$")
_BARE_RE = re.compile(r"^(?P<n>\d+|[ivxl]+|[IVXL]+)$")
_LEADER_RE = re.compile(r"(?:\.\s*){4,}$|…\s*$")

_ROMAN = ((50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def int_to_roman(n: int) -> str:
    out = ""
    for value, numeral in _ROMAN:
        while n >= value:
            out += numeral
            n -= value
    return out


def roman_to_int(token: str) -> int | None:
    """Canonical roman numerals up to 50 in one case ("iv", "IV"); anything else is None."""
    if not (token.islower() or token.isupper()):
        return None
    low = token.lower()
    for n in range(1, MAX_ROMAN + 1):
        if int_to_roman(n) == low:
            return n
    return None


@dataclass(frozen=True)
class Candidate:
    raw: str          # the printed text, e.g. "Page 7 of 41"
    system: str       # "arabic" | "roman"
    value: int
    total: int | None
    rule: str         # page | dashed | n_of_m | form | bare
    upper: bool = False

    @property
    def strong(self) -> bool:
        return self.rule != "bare"

    @property
    def number(self) -> str:
        if self.system == "arabic":
            return str(self.value)
        roman = int_to_roman(self.value)
        return roman.upper() if self.upper else roman


def _number(token: str) -> tuple[str, int, bool] | None:
    if token.isdigit():
        value = int(token)
        return ("arabic", value, False) if value >= 1 else None
    value = roman_to_int(token)
    return ("roman", value, token.isupper()) if value else None


def _candidate(raw: str, token: str, total: str | None, rule: str) -> Candidate | None:
    parsed = _number(token)
    if parsed is None:
        return None
    system, value, upper = parsed
    if total is not None:
        if system != "arabic" or not 1 <= value <= int(total):
            return None
        total_value = int(total)
    else:
        total_value = None
    return Candidate(raw, system, value, total_value, rule, upper)


def _match_line(line: str, previous: str | None, max_bare: int) -> Candidate | None:
    if m := _PAGE_RE.search(line):
        return _candidate(m["raw"], m["n"], m["m"], "page")
    if m := _DASHED_RE.match(line):
        return _candidate(line, m["n"], None, "dashed")
    if m := _N_OF_M_RE.match(line):
        return _candidate(line, m["n"], m["m"], "n_of_m")
    if m := _FORM_RE.search(line):
        return _candidate(m["raw"], m["n"], m["m"], "form")
    if m := _BARE_RE.match(line):
        if previous is not None and _LEADER_RE.search(previous):
            return None                       # a contents entry's page number, wrapped
        token = m["n"]
        if token.isdigit() and len(token) > MAX_BARE_DIGITS:
            return None                       # document-system ID
        found = _candidate(line, token, None, "bare")
        if found is None or found.value > max_bare:
            return None                       # a year, an ID, or not a numeral
        return found
    return None


def find_candidates(text: str, *, max_bare: int) -> list[Candidate]:
    """Every printed-number candidate in a page's header and footer lines, top to bottom.
    max_bare: the largest bare number accepted (the document's page count + 10)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    window = sorted(set(range(min(HEADER_LINES, len(lines))))
                    | set(range(max(0, len(lines) - FOOTER_LINES), len(lines))))
    found = []
    for i in window:
        candidate = _match_line(lines[i], lines[i - 1] if i else None, max_bare)
        if candidate is not None:
            found.append(candidate)
    return found


def find_label_candidate(text: str, *, max_bare: int) -> str | None:
    """The page's most likely printed number, as printed: the lowest strong candidate, else
    the lowest bare one. A guess for display; label_document() decides what is used."""
    found = find_candidates(text, max_bare=max_bare)
    strong = [c for c in found if c.strong]
    pick = (strong or found or [None])[-1]
    return pick.raw if pick else None


def unnumbered_label(sheet: int, sheets: int) -> str:
    """The convention for a page with no printed number. Still to be confirmed with the team
    (docs/DESIGN.md, open questions), so it lives in this one place."""
    return f"unnumbered, sheet {sheet} of {sheets}"


def _drop_inconsistent_forms(per_page: list[list[Candidate]], warnings: list[str]) -> None:
    forms = [(i, c) for i, cands in enumerate(per_page) for c in cands if c.rule == "form"]
    if not forms:
        return
    totals = {c.total for _, c in forms}
    offsets = {c.value - i for i, c in forms}
    if len(totals) == 1 and len(offsets) == 1:
        return
    sheets = ", ".join(str(i + 1) for i, _ in forms)
    reason = "totals differ" if len(totals) > 1 else "numbers do not climb by one"
    warnings.append(f"sheets {sheets}: form-style 'N of M' numbers ignored, {reason} [form_inconsistent]")
    for cands in per_page:
        cands[:] = [c for c in cands if c.rule != "form"]


def _choose_offset(per_page: list[list[Candidate]], system: str, sheets: range) -> int | None:
    """The offset (label = sheet index + offset) best supported by this system's candidates.
    Eligible only with a strong candidate behind it or at least two agreeing pages."""
    support: dict[int, set[int]] = {}
    strong: dict[int, set[int]] = {}
    for i in sheets:
        for c in per_page[i]:
            if c.system != system:
                continue
            offset = c.value - i
            support.setdefault(offset, set()).add(i)
            if c.strong:
                strong.setdefault(offset, set()).add(i)
    eligible = [o for o in support if strong.get(o) or len(support[o]) >= 2]
    if not eligible:
        return None
    return max(eligible, key=lambda o: (len(support[o]), len(strong.get(o, ())), -min(support[o])))


def label_document(pages: list[PageRecord]) -> tuple[list[PageRecord], list[str]]:
    """Label the pages of ONE document. Returns new records and warnings; never uses a page's
    position in its file as its label."""
    n = len(pages)
    if n == 0:
        return [], []
    warnings: list[str] = []
    per_page = [find_candidates(p.text, max_bare=n + BARE_SLACK) for p in pages]
    _drop_inconsistent_forms(per_page, warnings)

    arabic = _choose_offset(per_page, "arabic", range(n))
    run_start = n if arabic is None else min(n, max(0, 1 - arabic))
    roman = _choose_offset(per_page, "roman", range(run_start))
    roman_upper = any(c.upper for i in range(run_start) for c in per_page[i]
                      if c.system == "roman" and roman is not None and c.value == i + roman)

    labelled = []
    for i, page in enumerate(pages):
        system, offset = ("arabic", arabic) if i >= run_start else ("roman", roman)
        value = i + offset if offset is not None else 0
        if value < 1:
            label, source, accepted = unnumbered_label(i + 1, n), "unnumbered", None
        else:
            accepted = next((c for c in per_page[i] if c.system == system and c.value == value), None)
            label = str(value) if system == "arabic" else int_to_roman(value)
            if system == "roman" and roman_upper:
                label = label.upper()
            source = "printed" if accepted else "inferred"

        if accepted is None and per_page[i]:
            numbers = ", ".join(f"{c.number} [{c.rule}]" for c in per_page[i])
            sequenced = {s for s, o in (("arabic", arabic), ("roman", roman)) if o is not None}
            reason = ("sequence_mismatch" if any(c.system in sequenced for c in per_page[i])
                      else "unconfirmed")
            warnings.append(
                f"sheet {i + 1} (file position {page.position}): printed number(s) {numbers} "
                f"not used [{reason}]")

        labelled.append(replace(
            page, label=label, label_source=source,
            label_candidate=accepted.raw if accepted else page.label_candidate))
    return labelled, warnings
