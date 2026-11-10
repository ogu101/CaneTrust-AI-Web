"""
API-free tests for the workbook (docs/DESIGN.md, "Workbook"; milestone 6).

    .venv/bin/python test_workbook.py

Ports the rendering checks of the retired test_multi.py (overflow in every variable section,
headers, hidden columns, shifted sections, dropdowns, tallies, row heights, in-memory output)
onto the one template, and adds what the unified workbook needs: n = 0 chains, several chain
sheets copied from one template sheet, safe sheet names, the packet map and the contact rows.
All data is FICTIONAL.
"""

import io
import re
from datetime import date

from openpyxl import load_workbook

from testkit import chain_annotation, eval_tally

from annotate import ChainInput, Instrument
from annotation_schema import Finding, reconcile
from page_reader import PageRecord
from settings import load_settings
from workbook import (
    AMENDMENT_SLOTS, FIXED_ROWS, PACKET_MAP_SHEET, build_workbook, sheet_names,
)

SETTINGS = load_settings()
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


def instrument(tag, number, title, day, status="ACTIVE", labels=("1", "2")):
    pages = [PageRecord("f", i + 1, "invented words " * 10, label=lab, label_source="printed")
             for i, lab in enumerate(labels)]
    return Instrument(tag, number, f"d{number + 1}", title, "amendment" if number else "trust",
                      date.fromisoformat(day), status, False, pages)


BASE = instrument("Trust", 0, "Fictional Trust", "2001-01-01", "BASE")
A1 = instrument("Amendment 1", 1, "First Amendment", "2003-01-01", "REVOKED by Amendment 2")
A2 = instrument("Amendment 2", 2, "Second Amendment", "2004-01-01")
A3 = instrument("Amendment 3", 3, "Third Amendment", "2006-01-01")
CI3 = ChainInput("c1", "trust", "Fictional Trust", [BASE, A1, A2, A3], True)
CI0 = ChainInput("c2", "will", "Fictional Will", [instrument("Will", 0, "Fictional Will", "2010-01-01", "BASE")], True)

CONTACTS = [
    {"label": "Personal representative", "answer": "personal representative: Pat Example\n  address: 1 New St",
     "citation": "address: d9 (2025-06-27, p. 1), d7 (undated)", "status": "Ambiguous",
     "note": "Conflict in address for Pat Example: newest source preferred."},
    {"label": "Date of death", "answer": None, "citation": None, "status": "Not in this document", "note": None},
]


def build(specs, contacts=CONTACTS, packet_rows=(), chain_rows=(), notes=()):
    data = build_workbook(SETTINGS.template, specs, list(packet_rows), list(chain_rows), list(notes), contacts)
    return data, load_workbook(io.BytesIO(data))


# ---------------------------------------------------------------------------
# 1. A chain with three amendments, overflow in every variable section
# ---------------------------------------------------------------------------
ann = chain_annotation((1, 2, 3), n_terms=14, n_mentions=13, n_uncat=7, n_judgment=11, n_questions=8)
reconcile(ann, [1, 2, 3])
data, wb = build([{"name": "Fictional Trust", "annotation": ann, "input": CI3,
                   "notes": ["Superseded, not annotated: an earlier trust."]}])
ws = wb["Fictional Trust"]

r = FIXED_ROWS["bequest_language.um_receives"]
check(ws[f"B{r}"].value == "25% of the residue" and ws[f"D{r}"].value == "Found", "base block written")
check(ws[f"G{r}"].value == "No change", "amendment 1: 'No change'")
check(ws[f"H{r}"].value == "Changed: now 10% of the residue (Amendment 2, §3, p. 1)", "amendment 2: composed text")
check(ws[f"P{r}"].value == "10% of the residue" and ws[f"Q{r}"].value == "Amendment 2, §3, p. 1",
      "operative answer and controlling provision written")
r = FIXED_ROWS["bequest_language.trigger_event"]
check(ws[f"R{r}"].value == "Ambiguous — see note", "'Ambiguous' maps to the template's dropdown string")

check(ws["G11"].value == "Amendment 1: First Amendment (2003-01-01)\nchanges only — REVOKED by Amendment 2",
      "amendment 1 header: tag, title, date, form, and its revocation")
check(ws["H11"].value == "Amendment 2: Second Amendment (2004-01-01)\nfull restatement", "amendment 2 header")
check(ws["I11"].value == "Amendment 3: Third Amendment (2006-01-01)\nchanges only", "amendment 3 header")
check(ws["B10"].value == "TRUST (base instrument) — columns B–E", "the base block is headed with its tag")
check(ws["A1"].value.endswith("Trust chain — 3 amendments"), "sheet title names the family and count")
check(ws["B4"].value.startswith("Trust: Fictional Trust (2001-01-01); Amendment 1: First Amendment"),
      "the document line lists every instrument with its date")
check(ws["A3"].value == "Chain notes" and "Superseded" in ws["B3"].value, "chain notes are written at the top")

hidden = {k: d.hidden for k, d in ws.column_dimensions.items()}
check(not any(hidden.get(c) for c in "GHIOPQRST"), "used amendment columns and the operative block stay visible")
check(all(hidden.get(c) for c in "JKLMN"), "unused amendment columns (J-N) are hidden")


def find_row(sheet, prefix):
    for row in range(1, sheet.max_row + 1):
        v = sheet[f"A{row}"].value
        if isinstance(v, str) and v.startswith(prefix):
            return row
    raise AssertionError(f"row starting {prefix!r} not found")


band4, band5, band6, band7, band8, band9, band10 = (find_row(ws, p) for p in (
    "4 · DEFINED", "5 · ALL MENTIONS", "6 · FOUND", "7 · JUDGMENT", "8 · QUESTIONS", "9 · FEEDBACK", "10 · CONTACTS"))
check(band4 == 28, "section 4 header has not moved")
check(band5 == 41 + 2, "section 5 header moved down by the 2 inserted term rows")
check(band6 == 54 + 3, "section 6 header moved by terms + mentions inserts (3)")
check(band7 == 61 + 4, "section 7 header moved by 4")
check(band8 == 72 + 5, "section 8 header moved by 5")
check(band9 == 80 + 6, "section 9 header moved by 6")
check(band10 == band9 + 8, "section 10 (contacts) follows the 7 feedback rows")
check(ws[f"A{band4 + 14}"].value == "Term 14" and ws[f"B{band4 + 14}"].value == "definition 14",
      "14th defined term written into an inserted row")
check(ws[f"A{band5 + 1}"].value == "Mention 1" and ws[f"B{band5 + 1}"].value == "mention text 1",
      "mentions start right under their (shifted) header")
check(ws[f"A{band5 + 13}"].value == "Mention 13", "13th mention written into an inserted row")
check(ws[f"B{band6 + 7}"].value == "uncategorized 7", "7th uncategorized finding written")
check(ws[f"B{band7 + 11}"].value == "judgment call 11", "11th judgment call written")
check(ws[f"B{band8 + 8}"].value == "question 8?", "8th question written")
check(ws[f"B{band9 + 1}"].value is None, "feedback section stays empty")
check(ws.row_dimensions[band5].height == 19.5, "section header row kept its height after the shift")

# contacts
check(ws[f"A{band10 + 1}"].value == "Personal representative" and "Pat Example" in ws[f"B{band10 + 1}"].value,
      "contact rows: label and answer")
check("2025-06-27" in ws[f"C{band10 + 1}"].value and "Conflict" in ws[f"E{band10 + 1}"].value
      and ws[f"D{band10 + 1}"].value == "Ambiguous — see note",
      "contact rows: sources and dates in the citation column, conflicts in the note column")
check(ws[f"A{band10 + 2}"].value == "Date of death" and ws[f"D{band10 + 2}"].value == "Not in this document",
      "a fact not found says so")

# dropdowns and tallies follow the rows
dv = {re.match(r"[A-Z]+", str(d.sqref)).group(0): str(d.sqref) for d in ws.data_validations.dataValidation}
check(set(dv) == {"D", "R"}, "status dropdowns on the base and operative status columns")
check(f"D{band4 + 1}:D{band4 + 14}" in dv["D"] and f"R{band5 + 1}:R{band5 + 13}" in dv["R"],
      "the dropdowns cover the inserted term and mention rows")
check(f"D{band10 + 1}" not in dv["D"], "contact rows are outside the dropdowns")
check(ws["G9"].value == f'=COUNTIF(G13:G{band5 + 13},"*")-COUNTIF(G13:G{band5 + 13},"No change*")',
      "amendment tally formula extended to the last mention row")
check(eval_tally(ws, ws["G9"].value) == 1, "amendment 1 tally: 1 field changed (Added: tax-exempt)")
check(eval_tally(ws, ws["H9"].value) == 1, "amendment 2 tally: 1 (Changed: 10%)")
check(eval_tally(ws, ws["I9"].value) == 1, "amendment 3 tally: 1 (Ambiguous: trigger event)")
check(eval_tally(ws, ws["S9"].value) == 1, "operative tally counts the one 'Ambiguous — see note'")
fields = 13 + 14 + 13
check(eval_tally(ws, ws["B9"].value) == fields and eval_tally(ws, ws["F9"].value) == 0,
      "base tally: every field Found, none blank")
check(eval_tally(ws, ws["P9"].value) + eval_tally(ws, ws["S9"].value) == fields and eval_tally(ws, ws["T9"].value) == 0,
      "operative tally adds up")
before = eval_tally(ws, ws["C9"].value)
ws[f"D{band10 + 2}"] = "Not in this document"
check(eval_tally(ws, ws["C9"].value) == before, "contact statuses never reach the tallies")
check(ws.freeze_panes == "B12" and ws.print_title_rows == "$11:$11", "freeze panes and print titles re-applied")

# ---------------------------------------------------------------------------
# 2. n = 0: a will alone
# ---------------------------------------------------------------------------
ann0 = chain_annotation((), n_terms=1, n_mentions=0, base="Will", word="Codicil")
ann0.bequest_language.trigger_event.original = Finding(status="Ambiguous", answer="a / b", citation="Will, p. 1", note="n")
reconcile(ann0, [])
check(all(h.operative is not None and h.operative.answer == h.original.answer
          for h in [ann0.bequest_language.location, ann0.bequest_language.trigger_event]),
      "with no amendments, operative is the original")
data0, wb0 = build([{"name": "Will", "annotation": ann0, "input": CI0, "notes": []}])
w = wb0["Will"]
check(w["D19"].value == "Ambiguous — see note", "n = 0 maps Ambiguous to the dropdown string")
check(all(w.column_dimensions[c].hidden for c in "GHIJKLMNOPQRST") and not w.column_dimensions["F"].hidden,
      "n = 0: amendment columns and the operative block are hidden")
check(w["A1"].value.endswith("Will chain — 0 amendments") and w["B10"].value.startswith("WILL"),
      "n = 0: titled as a will chain")
check(w["A3"].value is None, "no chain notes, no notes row")
check(len(data0) > 5000 and isinstance(data0, bytes), "the workbook is built in memory as bytes")

# ---------------------------------------------------------------------------
# 3. Several chains in one workbook
# ---------------------------------------------------------------------------
names = sheet_names(["Fictional Trust", "Fictional Trust", "A/B: C?*[x]", "x" * 40, "Packet map", "", "'quoted'"])
check(names[0] == "Fictional Trust" and names[1] == "Fictional Trust (2)", "duplicate names get a suffix")
check(names[2] == "A B C x", "characters Excel forbids are removed")
check(len(names[3]) == 31 and all(len(n) <= 31 for n in names), "names are at most 31 characters")
check(names[4] == "Packet map (2)" and names[5] == "Chain 6" and names[6] == "quoted",
      "reserved, empty and quoted names are made safe")
check(len({n.lower() for n in names}) == len(names), "all names unique, ignoring case")

ann_b = chain_annotation((1,), n_terms=0, n_mentions=1)
reconcile(ann_b, [1])
CI1 = ChainInput("c3", "trust", "Other", [instrument("Trust", 0, "Other Trust", "1999-01-01", "BASE"),
                                          instrument("Amendment 1", 1, "Only Amendment", "2000-01-01")], True)
packet_rows = [{"file": "a.pdf", "sheets": "1-2", "id": "d1", "type": "trust", "title": "Fictional Trust",
                "date": "2001-01-01", "treatment": "annotate", "reason": "default", "chain": "c1 base", "flags": ""},
               {"file": "b.pdf", "sheets": "1", "id": "d9", "type": "accounting", "title": "Account",
                "date": "2025-06-27", "treatment": "log_only", "reason": "default", "chain": "",
                "flags": "possible_gift_in_non_annotated_document: gift answer unclear"}]
chain_rows = [{"id": "c1", "sheet": "Fictional Trust", "family": "trust", "base": "d1", "amendments": "d2, d3, d4",
               "result": "annotated"},
              {"id": "c9", "sheet": "(none)", "family": "will", "base": "d5", "amendments": "none",
               "result": "FAILED (rate_limited): wait"}]
data, wb = build([{"name": "Fictional Trust", "annotation": ann, "input": CI3, "notes": []},
                  {"name": "Will", "annotation": ann0, "input": CI0, "notes": []},
                  {"name": "Other", "annotation": ann_b, "input": CI1, "notes": []}],
                 packet_rows=packet_rows, chain_rows=chain_rows, notes=["c9: could not be annotated (rate_limited)"])
check(wb.sheetnames == ["Instructions", PACKET_MAP_SHEET, "Fictional Trust", "Will", "Other"],
      "Instructions once, the packet map, then one sheet per chain; the template sheet is gone")
for name in ("Fictional Trust", "Will", "Other"):
    s = wb[name]
    check(len(s.data_validations.dataValidation) == 2, f"{name}: the copy has its dropdowns")
    check(s.freeze_panes == "B12" and s.print_title_rows == "$11:$11", f"{name}: freeze panes and print titles")
    check(s["A13"].value == wb["Fictional Trust"]["A13"].value and s["A28"].value.startswith("4 ·"),
          f"{name}: template labels copied")
    widths = {c: s.column_dimensions[c].width for c in "ABGP"}
    check(widths == {"A": 52.0, "B": 44.0, "G": 40.0, "P": 44.0}, f"{name}: column widths copied")
t, wl, o = wb["Fictional Trust"], wb["Will"], wb["Other"]
check(not t.column_dimensions["G"].hidden and wl.column_dimensions["G"].hidden and not o.column_dimensions["G"].hidden
      and o.column_dimensions["H"].hidden and not t.column_dimensions["H"].hidden,
      "hidden columns are per sheet: hiding on one sheet never hides another")
check(eval_tally(o, o["G9"].value) == 1 and o["G9"].value.endswith('"No change*")'), "each sheet's tallies point at its own rows")
pm = wb[PACKET_MAP_SHEET]
header = [c.value for c in pm[3]][:10]
check(header == ["File", "Sheets", "Document", "Type", "Title", "Date", "Treatment", "Reason", "Chain", "Flags"],
      "packet map columns")
check(pm["A4"].value == "a.pdf" and pm["G5"].value == "log_only" and "possible_gift" in pm["J5"].value,
      "packet map rows: every document with treatment and flags")
cells = [c.value for row in pm.iter_rows() for c in row if c.value]
check("FAILED (rate_limited): wait" in cells and "c9: could not be annotated (rate_limited)" in cells,
      "a failed chain is listed in the packet map with its note")
check(not any("Contacts and court facts" == v for v in cells), "with chain sheets, contacts live on the chain sheets")
data, wb = build([], packet_rows=packet_rows, chain_rows=[], notes=[])
check(wb.sheetnames == ["Instructions", PACKET_MAP_SHEET], "no chains: Instructions and the packet map only")
check(any(c.value == "Contacts and court facts (whole packet)" for row in wb[PACKET_MAP_SHEET].iter_rows() for c in row),
      "no chains: the contacts go on the packet map")
check(AMENDMENT_SLOTS == 8, "eight amendment columns")

print(f"\nALL {passed} CHECKS PASSED")
