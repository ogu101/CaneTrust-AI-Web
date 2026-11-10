"""
The workbook (docs/DESIGN.md, "Workbook"): one per matter.

    build_workbook(name, chains, documents, ...) -> bytes

Sheets: the template's Instructions once; a "Packet map" listing every document; then one sheet
per annotated chain, copied from the template's Annotation sheet and named from its base title
(at most 31 characters, unique, no characters Excel forbids).

Each chain sheet has three column blocks per row: the base instrument (B-E), one column per
amendment or codicil (G-N, 8 slots), and the operative position (P-S). Unused amendment columns
are hidden; with no amendments, the amendment columns and the operative block (G-T) are hidden.
Sections 4-8 grow when the model finds more items than the template has rows; later sections
shift down and the Status dropdowns, tally formulas and row heights follow. A tenth section at
the bottom holds the facts lane's contact rows: sources and dates in the citation column (C),
conflicts in the note column (E). Facts never touch sections 1-3.

openpyxl's copy_worksheet drops data validations, conditional formatting, freeze panes and print
titles, and keeps a run of equal-width columns as one entry; every copy gets them re-applied here.

Cell anchors were read from assets/MiLA_Annotation_Template.xlsx. If the template changes,
change the anchors below.
"""

from __future__ import annotations

import copy
import datetime as dt
import io
import math
import re

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation

from annotation_schema import AmendmentEntry, ChainAnnotation, FieldHistory

# ----------------------------------------------------------------------------
# Template anchors (sheet "Annotation")
# ----------------------------------------------------------------------------
TEMPLATE_SHEET = "Annotation"
INSTRUCTIONS_SHEET = "Instructions"
PACKET_MAP_SHEET = "Packet map"

HEADER = {"title": "A1", "notes_label": "A3", "notes": "B3", "document_name": "B4",
          "annotator": "B5", "date_completed": "B6", "hours": "B7", "base_header": "B10"}

FIXED_ROWS = {
    "bequest_language.location": 13,
    "bequest_language.verbatim_text": 14,
    "bequest_language.plain_english_summary": 15,
    "bequest_language.trustee_executor_names": 16,
    "bequest_language.trustee_executor_contact": 17,
    "bequest_language.um_receives": 18,
    "bequest_language.trigger_event": 19,
    "income_beneficiaries.count": 21,
    "income_beneficiaries.names_and_relationship": 22,
    "income_beneficiaries.timing_implication": 23,
    "restrictions.primary_or_contingent": 25,
    "restrictions.conditions_precedent": 26,
    "restrictions.other_restrictions": 27,
}
FIXED_INPUT_RANGES = [(13, 19), (21, 23), (25, 27)]

# (first placeholder row, placeholder count) in the unmodified template; each band row is the
# row just above first_row.
VARIABLE_SECTIONS = {
    "defined_terms": dict(first_row=29, placeholder_count=12),
    "um_mentions": dict(first_row=42, placeholder_count=12),
    "found_but_uncategorized": dict(first_row=55, placeholder_count=6),
    "judgment_calls": dict(first_row=62, placeholder_count=10),
    "questions_for_client": dict(first_row=73, placeholder_count=7),
    "feedback": dict(first_row=81, placeholder_count=7),
}

BASE_COLS = dict(answer="B", citation="C", status="D", note="E")
OPERATIVE_COLS = dict(answer="P", citation="Q", status="R", note="S")
AMENDMENT_FIRST_COL = 7            # G
AMENDMENT_SLOTS = 8                # G..N
LAST_COL = 20                      # T
AMENDMENT_HEADER_ROW = 11
TALLY_ROW = 9

STATUS_TO_CELL = {
    "Found": "Found",
    "Not in this document": "Not in this document",
    "Could not determine": "Could not determine",
    "Ambiguous": "Ambiguous — see note",
}
STATUS_LIST = '"Found,Not in this document,Could not determine,Ambiguous — see note"'
FORM_TO_TEXT = {"changes_only": "changes only", "restating": "full restatement", "unclear": "form unclear"}
FAMILY_TITLE = {"trust": "Trust", "will": "Will", "other": "Single instrument"}

BAND_FILL = "FFEDF2F0"          # section header rows; autofit skips them
LINE_HEIGHT = 12.75
MAX_ROW_HEIGHT = 409.5
ANNOTATOR = "MiLA Assistant (LLM draft — attorney review required)"

_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
RESERVED_NAMES = {INSTRUCTIONS_SHEET.lower(), PACKET_MAP_SHEET.lower(), TEMPLATE_SHEET.lower()}


# ----------------------------------------------------------------------------
# Sheet names
# ----------------------------------------------------------------------------
def sheet_names(wanted: list[str]) -> list[str]:
    """Excel-safe, unique (case-insensitively), at most 31 characters, never a reserved name."""
    taken = set(RESERVED_NAMES)
    out = []
    for i, raw in enumerate(wanted, start=1):
        base = " ".join(_INVALID_SHEET_CHARS.sub(" ", raw or "").split()).strip("'") or f"Chain {i}"
        name, k = base[:31].rstrip(), 1
        while name.lower() in taken:
            k += 1
            suffix = f" ({k})"
            name = base[:31 - len(suffix)].rstrip() + suffix
        taken.add(name.lower())
        out.append(name)
    return out


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _amendment_col(index: int) -> str:
    return get_column_letter(AMENDMENT_FIRST_COL + index)


def amendment_text(entry: AmendmentEntry) -> str:
    if entry.effect == "No change":
        return "No change"
    text = f"{entry.effect}:"
    if entry.description.strip():
        text += f" {entry.description.strip()}"
    if entry.citation.strip():
        text += f" ({entry.citation.strip()})"
    return text


def _write_block(ws, row: int, finding, cols: dict) -> None:
    ws[f"{cols['answer']}{row}"] = finding.answer
    ws[f"{cols['citation']}{row}"] = finding.citation
    ws[f"{cols['status']}{row}"] = STATUS_TO_CELL.get(finding.status, finding.status)
    ws[f"{cols['note']}{row}"] = finding.note


def _write_history(ws, row: int, history: FieldHistory, numbers: list[int]) -> None:
    _write_block(ws, row, history.original, BASE_COLS)
    by_number = {e.amendment_number: e for e in history.amendments}
    for index, number in enumerate(numbers):
        entry = by_number.get(number)
        ws[f"{_amendment_col(index)}{row}"] = (
            amendment_text(entry) if entry else "Could not determine: no entry for this amendment.")
    if history.operative is not None:
        _write_block(ws, row, history.operative, OPERATIVE_COLS)


def _copy_row_style(ws, src_row: int, dst_row: int) -> None:
    for col in range(1, LAST_COL + 1):
        src, dst = ws.cell(row=src_row, column=col), ws.cell(row=dst_row, column=col)
        dst.font = copy.copy(src.font)
        dst.fill = copy.copy(src.fill)
        dst.border = copy.copy(src.border)
        dst.alignment = copy.copy(src.alignment)
        dst.number_format = src.number_format


def _insert_rows(ws, at_row: int, amount: int, style_src_row: int) -> None:
    """insert_rows moves cells but not row heights or merged ranges below; carry them over."""
    heights = {r: ws.row_dimensions[r].height for r in range(at_row, ws.max_row + 1)}
    ws.insert_rows(at_row, amount)
    for r, h in sorted(heights.items(), reverse=True):
        ws.row_dimensions[r + amount].height = h
    for i in range(amount):
        ws.row_dimensions[at_row + i].height = ws.row_dimensions[style_src_row].height
        _copy_row_style(ws, style_src_row, at_row + i)


def _ensure_capacity(ws, key: str, n_items: int, offset: int):
    cfg = VARIABLE_SECTIONS[key]
    first = cfg["first_row"] + offset
    capacity = cfg["placeholder_count"]
    inserted = max(0, n_items - capacity)
    if inserted:
        _insert_rows(ws, first + capacity, inserted, style_src_row=first + capacity - 1)
    return first, capacity + inserted, inserted


def ungroup_columns(ws) -> None:
    """A run of equal-width columns is saved as ONE <col min=7 max=14>, loaded under the first
    column's key only. Hiding one column inside such a run writes overlapping definitions and
    makes Excel 'repair' the file, so split every run into one entry per column first."""
    for dim in list(ws.column_dimensions.values()):
        if dim.min and dim.max and dim.max > dim.min:
            low, high = dim.min, dim.max
            dim.max = low
            for index in range(low + 1, high + 1):
                letter = get_column_letter(index)
                ws.column_dimensions[letter].width = dim.width
                ws.column_dimensions[letter].hidden = dim.hidden


def _autofit_row(ws, row: int, text_cols: list[str]) -> None:
    if ws[f"A{row}"].fill.fgColor.rgb == BAND_FILL:
        return
    lines = 1
    for col in text_cols:
        value = ws[f"{col}{row}"].value
        if not value:
            continue
        width = ws.column_dimensions[col].width or 8.43
        per_line = max(1, int(width))
        needed = sum(max(1, math.ceil(len(part) / per_line)) for part in str(value).split("\n"))
        lines = max(lines, needed)
    current = ws.row_dimensions[row].height or 15
    ws.row_dimensions[row].height = min(MAX_ROW_HEIGHT, max(current, lines * LINE_HEIGHT + 4))


def _ranges_formula(func: str, col: str, ranges, criterion: str | None) -> str:
    args = (lambda rng: f'{rng},"{criterion}"') if criterion is not None else (lambda rng: rng)
    return "=" + "+".join(f"{func}({args(f'{col}{lo}:{col}{hi}')})" for lo, hi in ranges)


# ----------------------------------------------------------------------------
# One chain sheet
# ----------------------------------------------------------------------------
def restore_sheet_machinery(ws, template_ws) -> None:
    """What copy_worksheet leaves behind: freeze panes, print titles, conditional formatting,
    column widths and hidden flags (validations are rebuilt by render_chain_sheet)."""
    ws.freeze_panes = template_ws.freeze_panes
    ws.print_title_rows = template_ws.print_title_rows
    for rng in template_ws.conditional_formatting:
        for rule in rng.rules:
            ws.conditional_formatting.add(str(rng.sqref), copy.copy(rule))
    for key, dim in template_ws.column_dimensions.items():
        ws.column_dimensions[key].width = dim.width
        ws.column_dimensions[key].hidden = dim.hidden


def render_chain_sheet(ws, annotation: ChainAnnotation, ci, *, notes: list[str],
                       contacts: list[dict], annotator: str = ANNOTATOR) -> None:
    """ci: annotate.ChainInput. notes: chain-level notes (superseded, gaps, checks)."""
    numbers = ci.amendment_numbers
    n = len(numbers)
    if n > AMENDMENT_SLOTS:
        raise ValueError(f"Expected at most {AMENDMENT_SLOTS} amendments, got {n}")

    ws[HEADER["title"]] = (f"Document Annotation Sheet — {FAMILY_TITLE.get(ci.family, 'Instrument')} chain"
                           f" — {n} amendment{'s' if n != 1 else ''}")
    ws[HEADER["document_name"]] = "; ".join(
        f"{i.tag}: {i.title} ({i.date.isoformat() if i.date else 'undated'})" for i in ci.instruments) \
        or annotation.document_name
    ws[HEADER["annotator"]] = annotator
    ws[HEADER["date_completed"]] = dt.date.today().isoformat()
    base_tag = ci.instruments[0].tag if ci.has_base else "BASE INSTRUMENT (NOT PROVIDED)"
    ws[HEADER["base_header"]] = f"{base_tag.upper()} (base instrument) — columns B–E"
    if notes:
        ws[HEADER["notes_label"]] = "Chain notes"
        ws[HEADER["notes_label"]].font = Font(bold=True)
        ws[HEADER["notes"]] = "\n".join(notes)
        ws[HEADER["notes"]].alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells("B3:E3")
        ws.row_dimensions[3].height = min(MAX_ROW_HEIGHT, LINE_HEIGHT * (len(notes) + 1) + 4)

    # Amendment column headers; hide what this chain does not use.
    info = {i.amendment_number: i for i in annotation.instruments}
    revoked_by: dict[int, str] = {}
    for item in annotation.instruments:
        for r in item.revokes:
            revoked_by.setdefault(r, f"Amendment {item.amendment_number}")
    for index, inst in enumerate(ci.amendments):
        form = FORM_TO_TEXT.get(info[inst.number].form, "form unclear") if inst.number in info else "form unclear"
        header = f"{inst.tag}: {inst.title} ({inst.date.isoformat() if inst.date else 'undated'})\n{form}"
        if inst.status.startswith("REVOKED"):
            header += f" — {inst.status}"
        elif inst.number in revoked_by:
            header += f" — REVOKED by {revoked_by[inst.number]} (per the model)"
        if inst.revival_assumed:
            header += " — revival assumed"
        ws.cell(row=AMENDMENT_HEADER_ROW, column=AMENDMENT_FIRST_COL + index).value = header
    if n:
        ws.row_dimensions[AMENDMENT_HEADER_ROW].height = 48
    for index in range(n, AMENDMENT_SLOTS):
        ws.column_dimensions[_amendment_col(index)].hidden = True
    if n == 0:
        for col in range(AMENDMENT_FIRST_COL, LAST_COL + 1):
            ws.column_dimensions[get_column_letter(col)].hidden = True

    # Sections 1-3: one row per field.
    for path, row in FIXED_ROWS.items():
        obj = annotation
        for part in path.split("."):
            obj = getattr(obj, part)
        _write_history(ws, row, obj, numbers)

    # Sections 4-8: variable length, each shifted by the rows inserted above it.
    offset = 0
    first, total, inserted = _ensure_capacity(ws, "defined_terms", len(annotation.defined_terms), offset)
    terms = (first, first + total - 1)
    for i in range(total):
        row = first + i
        if i < len(annotation.defined_terms):
            ws[f"A{row}"] = annotation.defined_terms[i].term
            _write_history(ws, row, annotation.defined_terms[i].history, numbers)
        else:
            ws[f"{BASE_COLS['status']}{row}"] = STATUS_TO_CELL["Not in this document"]
            ws[f"{OPERATIVE_COLS['status']}{row}"] = STATUS_TO_CELL["Not in this document"]
    offset += inserted

    first, total, inserted = _ensure_capacity(ws, "um_mentions", len(annotation.um_mentions), offset)
    mentions = (first, first + total - 1)
    for i in range(total):
        row = first + i
        if i < len(annotation.um_mentions):
            ws[f"A{row}"] = f"Mention {i + 1}"
            _write_history(ws, row, annotation.um_mentions[i], numbers)
        else:
            ws[f"{BASE_COLS['status']}{row}"] = STATUS_TO_CELL["Not in this document"]
            ws[f"{OPERATIVE_COLS['status']}{row}"] = STATUS_TO_CELL["Not in this document"]
    offset += inserted
    last_history_row = mentions[1]

    first, total, inserted = _ensure_capacity(
        ws, "found_but_uncategorized", len(annotation.found_but_uncategorized), offset)
    for i, item in enumerate(annotation.found_but_uncategorized):
        row = first + i
        ws[f"A{row}"] = f"{i + 1}."
        ws[f"B{row}"] = item.text
        ws[f"C{row}"] = item.citation
        ws[f"E{row}"] = item.note
    offset += inserted

    for key, items in (("judgment_calls", annotation.judgment_calls),
                       ("questions_for_client", annotation.questions_for_client)):
        first, total, inserted = _ensure_capacity(ws, key, len(items), offset)
        for i, text in enumerate(items):
            ws[f"A{first + i}"] = f"{i + 1}."
            ws[f"B{first + i}"] = text
        offset += inserted

    feedback_last = VARIABLE_SECTIONS["feedback"]["first_row"] + offset + VARIABLE_SECTIONS["feedback"]["placeholder_count"] - 1
    _write_contacts(ws, contacts, band_src=VARIABLE_SECTIONS["feedback"]["first_row"] + offset - 1,
                    row_src=VARIABLE_SECTIONS["feedback"]["first_row"] + offset, at=feedback_last + 1)

    # Status dropdowns and tallies cover exactly the status cells of sections 1-5.
    inputs = list(FIXED_INPUT_RANGES) + [terms, mentions]
    ws.data_validations.dataValidation = []
    for col in (BASE_COLS["status"], OPERATIVE_COLS["status"]):
        dv = DataValidation(type="list", formula1=STATUS_LIST, allow_blank=True)
        dv.sqref = MultiCellRange(" ".join(f"{col}{lo}:{col}{hi}" for lo, hi in inputs))
        ws.add_data_validation(dv)
    for status_col, cells in ((BASE_COLS["status"], "BCDE"), (OPERATIVE_COLS["status"], "PQRS")):
        for cell_col, value in zip(cells, ("Found", "Not in this document", "Could not determine",
                                           STATUS_TO_CELL["Ambiguous"])):
            ws[f"{cell_col}{TALLY_ROW}"] = _ranges_formula("COUNTIF", status_col, inputs, value)
        blank_col = "F" if status_col == BASE_COLS["status"] else "T"
        ws[f"{blank_col}{TALLY_ROW}"] = _ranges_formula("COUNTBLANK", status_col, inputs, None)
    for index in range(n):
        col = _amendment_col(index)
        rng = f"{col}13:{col}{last_history_row}"
        ws[f"{col}{TALLY_ROW}"] = f'=COUNTIF({rng},"*")-COUNTIF({rng},"No change*")'

    text_cols = (list(BASE_COLS.values()) + [_amendment_col(i) for i in range(n)]
                 + list(OPERATIVE_COLS.values()))
    for row in range(13, ws.max_row + 1):
        _autofit_row(ws, row, text_cols)


def _write_contacts(ws, contacts: list[dict], *, band_src: int, row_src: int, at: int) -> None:
    """Section 10 at the bottom: facts-lane contact rows, styled like the rows above."""
    _copy_row_style(ws, band_src, at)
    ws.row_dimensions[at].height = ws.row_dimensions[band_src].height
    ws[f"A{at}"] = "10 · CONTACTS AND COURT FACTS (whole packet)"
    ws[f"B{at}"] = ("From every document in the packet (facts lane), newest source first. Sources and "
                    "dates in column C, conflicts in column E. Never used for sections 1–3.")
    for i, row in enumerate(contacts or [{"label": "No contacts found", "answer": None, "citation": None,
                                          "status": "Not in this document", "note": None}], start=1):
        r = at + i
        _copy_row_style(ws, row_src, r)
        ws[f"A{r}"] = row["label"]
        ws[f"B{r}"] = row.get("answer")
        ws[f"C{r}"] = row.get("citation")
        ws[f"D{r}"] = STATUS_TO_CELL.get(row.get("status"), row.get("status"))
        ws[f"E{r}"] = row.get("note")
        for col in "BCE":
            ws[f"{col}{r}"].alignment = Alignment(wrap_text=True, vertical="top")


# ----------------------------------------------------------------------------
# Packet map
# ----------------------------------------------------------------------------
PACKET_MAP_COLUMNS = (("File", 34), ("Sheets", 10), ("Document", 10), ("Type", 22), ("Title", 40),
                      ("Date", 12), ("Treatment", 12), ("Reason", 36), ("Chain", 30), ("Flags", 60))


def _write_packet_map(ws, rows: list[dict], chain_rows: list[dict], notes: list[str],
                      contacts: list[dict] | None) -> None:
    head_fill = PatternFill("solid", fgColor=BAND_FILL)
    ws["A1"] = "Packet map — every document in the packet"
    ws["A1"].font = Font(bold=True, size=13)
    for c, (title, width) in enumerate(PACKET_MAP_COLUMNS, start=1):
        cell = ws.cell(row=3, column=c, value=title)
        cell.font, cell.fill = Font(bold=True), head_fill
        ws.column_dimensions[get_column_letter(c)].width = width
    keys = ("file", "sheets", "id", "type", "title", "date", "treatment", "reason", "chain", "flags")
    r = 4
    for row in rows:
        for c, key in enumerate(keys, start=1):
            cell = ws.cell(row=r, column=c, value=row.get(key))
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    ws.freeze_panes = "A4"
    r += 1
    ws.cell(row=r, column=1, value="Chains").font = Font(bold=True)
    for c, title in enumerate(("Chain", "Sheet", "Family", "Base", "Amendments", "Result"), start=1):
        cell = ws.cell(row=r + 1, column=c, value=title)
        cell.font, cell.fill = Font(bold=True), head_fill
    r += 2
    for row in chain_rows:
        for c, key in enumerate(("id", "sheet", "family", "base", "amendments", "result"), start=1):
            ws.cell(row=r, column=c, value=row.get(key)).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    if notes:
        r += 1
        ws.cell(row=r, column=1, value="Notes").font = Font(bold=True)
        for note in notes:
            r += 1
            ws.cell(row=r, column=1, value=note).alignment = Alignment(wrap_text=False)
    if contacts is not None:
        r += 2
        ws.cell(row=r, column=1, value="Contacts and court facts (whole packet)").font = Font(bold=True)
        for c, title in enumerate(("Item", "Answer", "Sources (date)", "Status", "Conflicts"), start=1):
            cell = ws.cell(row=r + 1, column=c, value=title)
            cell.font, cell.fill = Font(bold=True), head_fill
        r += 2
        for row in contacts:
            for c, key in enumerate(("label", "answer", "citation", "status", "note"), start=1):
                ws.cell(row=r, column=c, value=row.get(key)).alignment = Alignment(wrap_text=True, vertical="top")
            r += 1


# ----------------------------------------------------------------------------
# The whole workbook
# ----------------------------------------------------------------------------
def build_workbook(template_path, sheets: list[dict], packet_rows: list[dict], chain_rows: list[dict],
                   notes: list[str], contacts: list[dict]) -> bytes:
    """sheets: [{"name", "annotation", "input", "notes"}] for every annotated chain, in order.
    chain_rows already carry each chain's sheet name or failure. Returns xlsx bytes."""
    wb = load_workbook(template_path)
    template_ws = wb[TEMPLATE_SHEET]
    ungroup_columns(template_ws)
    for spec in sheets:
        ws = wb.copy_worksheet(template_ws)
        ws.title = spec["name"]
        restore_sheet_machinery(ws, template_ws)
        render_chain_sheet(ws, spec["annotation"], spec["input"], notes=spec["notes"], contacts=contacts)
    wb.remove(template_ws)
    packet = wb.create_sheet(PACKET_MAP_SHEET, index=1)
    _write_packet_map(packet, packet_rows, chain_rows, notes, contacts if not sheets else None)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
