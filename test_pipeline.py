"""
API-free end-to-end tests of the unified pipeline (service.py; milestone 6).

    .venv/bin/python test_pipeline.py

Every model call (triage, facts, annotation) goes to testkit.StubModel, so the real code runs
from file bytes to workbook bytes. Inputs are the FICTIONAL Marlowe PDFs and invented .txt files
(pages split on form feeds). Each run reopens the workbook and checks its sheets, hidden columns,
packet map, dropdowns and tallies. Also: plan validation, auto-confirm, and the CLI.
"""

import contextlib
import io
import json
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

import anthropic
import httpx
from openpyxl import load_workbook

import testkit
from testkit import StubModel, eval_tally, install_stub

import service
from page_reader import ExtractionError
from plan import PLAN_VERSION, validate_plan
from service import NeedsConfirmation, UploadedFile, annotate_one_shot, annotate_plan, load_files, triage_packet
from settings import load_settings
from workbook import PACKET_MAP_SHEET

SETTINGS = load_settings()
FIX = Path(__file__).parent / "fixtures"
MARKER = "Zephyrquill"
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


def marlowe(*indices):
    files = sorted(FIX.glob("0*.pdf"))
    return [UploadedFile(files[i].name, files[i].read_bytes()) for i in indices]


def txt(name, when, pages=2, extra=""):
    body = []
    for n in range(1, pages + 1):
        head = f"{MARKER} specimen instrument executed {when}." if n == 1 else f"{MARKER} continued."
        body.append(f"{head}\nInvented words for testing only, nothing real here at all.\n{extra}\nPage {n}")
    return UploadedFile(name, "\f".join(body).encode())


def run(files, stub=None, **kw):
    stub = install_stub(service, stub or StubModel())
    messages = []
    result = annotate_one_shot(files, SETTINGS, progress=messages.append, **kw)
    return result, load_workbook(io.BytesIO(result.xlsx)), stub, messages


def packet_rows(wb):
    pm = wb[PACKET_MAP_SHEET]
    rows = []
    for r in range(4, pm.max_row + 1):
        if pm[f"A{r}"].value in (None, "Chains"):
            break
        rows.append({k: pm.cell(row=r, column=c).value for c, k in enumerate(
            ("file", "sheets", "id", "type", "title", "date", "treatment", "reason", "chain", "flags"), start=1)})
    return rows


def all_values(ws):
    return [c.value for row in ws.iter_rows() for c in row if c.value is not None]


def sheet_ok(ws, n_amendments):
    """Dropdowns, tallies and hidden columns on one chain sheet."""
    dvs = ws.data_validations.dataValidation
    ok = len(dvs) == 2 and ws.freeze_panes == "B12"
    ok &= eval_tally(ws, ws["B9"].value) + eval_tally(ws, ws["C9"].value) + eval_tally(ws, ws["D9"].value) \
        + eval_tally(ws, ws["E9"].value) + eval_tally(ws, ws["F9"].value) > 13
    hidden = {c for c in "GHIJKLMNOPQRST" if ws.column_dimensions[c].hidden}
    if n_amendments == 0:
        ok &= hidden == set("GHIJKLMNOPQRST")
    else:
        ok &= hidden == set("GHIJKLMN"[n_amendments:])
        ok &= all(isinstance(eval_tally(ws, ws[f"{c}9"].value), int) for c in "GHIJKLMN"[:n_amendments])
    return ok


SURFACES = []

# ---------------------------------------------------------------------------
# 1. A trust alone (n = 0)
# ---------------------------------------------------------------------------
result, wb, stub, messages = run(marlowe(0))
check(wb.sheetnames == ["Instructions", PACKET_MAP_SHEET, "00 marlowe trust original"], f"trust alone: sheets {wb.sheetnames}")
ws = wb["00 marlowe trust original"]
check(sheet_ok(ws, 0), "trust alone: dropdowns, tallies, and amendment + operative columns hidden")
check(result.filename == "00_marlowe_trust_original_annotation.xlsx" and result.route == "unified", "file name and route")
check([len(stub.calls(t)) for t in ("record_triage", "record_facts", "analyze_documents")] == [1, 1, 1],
      "one triage call, one facts call, one annotation call")
ann_msg = stub.calls("analyze_documents")[0]["messages"][0]["content"]
check("[Page 1]" in ann_msg and "[Page 2]" in ann_msg and "[PDF page" not in ann_msg and "UNIVERSITY OF MIAMI" in ann_msg,
      "the model receives the text with printed page labels, not PDF positions")
check("This chain has no amendments" in ann_msg, "n = 0 is the no-amendment case of the one path")
rows = packet_rows(wb)
check(len(rows) == 1 and rows[0]["chain"] == "c1 base" and rows[0]["treatment"] == "annotate", "packet map: one row")
SURFACES += messages + result.warnings

# ---------------------------------------------------------------------------
# 2. A trust with amendments, uploaded out of order (restatement resets the chain)
# ---------------------------------------------------------------------------
result, wb, stub, messages = run(marlowe(4, 2, 0, 3, 1))
check(len(wb.sheetnames) == 3, "one chain sheet")
ws = wb[wb.sheetnames[2]]
check(sheet_ok(ws, 1), "restated chain: one amendment column visible, the rest hidden")
check(ws["G11"].value.startswith("Amendment 1: 04 marlowe fourth amendment (2016-10-17)"),
      "the amendment column is the 2016 amendment, by date, not upload order")
check(ws["B10"].value.startswith("TRUST") and "03 marlowe third amendment" in ws["B4"].value,
      "the 2013 restatement is the base")
check("Superseded, not annotated" in ws["B3"].value and ws["B3"].value.count("Superseded") == 3,
      "the three earlier instruments are noted as superseded, not missing")
rows = {r["id"]: r for r in packet_rows(wb)}
check([r["file"] for r in packet_rows(wb)][:2] == ["04_marlowe_fourth_amendment.pdf", "02_marlowe_second_amendment.pdf"],
      "packet map: documents in upload (file) order")
check(rows["d3"]["chain"].startswith("c1 superseded") and rows["d4"]["chain"] == "c1 base" and rows["d1"]["chain"] == "c1 Amendment 1",
      "packet map: each document's place in the chain")
msg = stub.calls("analyze_documents")[0]["messages"][0]["content"]
check(msg.index("=== Trust [BASE]") < msg.index("=== Amendment 1 [ACTIVE]") and "Superseded by the base" in msg,
      "the model reads base then amendment, told what was superseded")
SURFACES += messages + result.warnings

# ---------------------------------------------------------------------------
# 3. A will with a codicil, and 4. a mixed packet with facts
# ---------------------------------------------------------------------------
WILL = txt("last_will.txt", "May 1, 2001", pages=3)
CODICIL = txt("codicil_one.txt", "June 2, 2005", pages=1)
result, wb, stub, messages = run([CODICIL, WILL])
ws = wb[wb.sheetnames[2]]
check(sheet_ok(ws, 1) and ws["G11"].value.startswith("Codicil 1: codicil one (2005-06-02)") and ws["B10"].value.startswith("WILL"),
      "will chain: the codicil amends the will")
check("Document family: will" in stub.calls("analyze_documents")[0]["messages"][0]["content"], "the model is told it is a will")

NOTICE = txt("notice_of_admin.txt", "July 3, 2024", pages=1, extra="Personal Representative, 1 Example Way, ZZ 00000")
ACCOUNT = txt("annual_account.txt", "August 4, 2025", pages=2)


def facts_from_notice(request):
    m = re.search(r"=== Document (d\d+) \(notice_of_administration", request["messages"][0]["content"])
    return {"facts": [{"field": "personal_representative", "subject": "unknown", "value": "Pat Example",
                       "document_id": m.group(1), "page": "1"},
                      {"field": "address", "subject": "Pat Example", "value": "1 Example Way",
                       "document_id": m.group(1), "page": "1"}]}


files = marlowe(0, 1) + [WILL, CODICIL, NOTICE, ACCOUNT]
result, wb, stub, messages = run(files, StubModel({"record_facts": facts_from_notice}))
check(len(wb.sheetnames) == 4, f"mixed: a trust chain and a will chain: {wb.sheetnames}")
trust_ws, will_ws = wb[wb.sheetnames[2]], wb[wb.sheetnames[3]]
check(sheet_ok(trust_ws, 1) and sheet_ok(will_ws, 1), "mixed: both sheets have their machinery")
check(len(stub.calls("analyze_documents")) == 2 and len(stub.calls("record_facts")) == 1, "two chains, one facts call")
rows = packet_rows(wb)
check([r["treatment"] for r in rows] == ["annotate"] * 4 + ["facts_only", "log_only"],
      "packet map: every document with its treatment")
check(not rows[4]["chain"] and not rows[5]["chain"], "notices and accountings are not chained")
facts_msg = stub.calls("record_facts")[0]["messages"][0]["content"]
check("notice_of_administration, 2024-07-03, full text" in facts_msg, "the facts lane reads the notice in full")
for sheet in (trust_ws, will_ws):
    values = all_values(sheet)
    band = next(r for r in range(1, sheet.max_row + 1) if str(sheet[f"A{r}"].value).startswith("10 ·"))
    check(sheet[f"A{band + 1}"].value == "Personal representative" and "Pat Example" in sheet[f"B{band + 1}"].value
          and "2024-07-03" in sheet[f"C{band + 1}"].value, f"{sheet.title}: contact row with source and date")
    check("Pat Example" not in str(sheet["B16"].value) and "Pat Example" not in str(sheet["B17"].value),
          f"{sheet.title}: facts never feed the instrument's own fields")
SURFACES += messages + result.warnings

# ---------------------------------------------------------------------------
# 5. One chain fails; then every chain fails
# ---------------------------------------------------------------------------
req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class FailWill(StubModel):
    def stream(self, **request):
        if request["tools"][0]["name"] == "analyze_documents" and "Document family: will" in request["messages"][0]["content"]:
            raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
        return super().stream(**request)


result, wb, stub, messages = run(files, FailWill())
check(len(wb.sheetnames) == 3 and wb.sheetnames[2].startswith("00 marlowe"), "the trust chain survives the will chain's failure")
values = all_values(wb[PACKET_MAP_SHEET])
check(any(isinstance(v, str) and v.startswith("FAILED (rate_limited)") for v in values),
      "the packet map says which chain failed and why")
check(any("could not be annotated (rate_limited)" in str(v) for v in values) and any("could not be annotated" in m for m in messages),
      "the failure is a note and a progress message")


class FailAll(StubModel):
    def stream(self, **request):
        if request["tools"][0]["name"] == "analyze_documents":
            raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
        return super().stream(**request)


try:
    run(marlowe(0), FailAll())
    check(False, "every chain failing must raise")
except anthropic.RateLimitError:
    check(True, "when every chain fails, the error is raised for the caller to report")

result, wb, stub, messages = run(files, StubModel({"record_facts": RuntimeError("facts down")}))
check(len(wb.sheetnames) == 4 and any("facts lane failed" in str(v) for v in all_values(wb[wb.sheetnames[2]])),
      "a failed facts lane leaves a note, not a failed run")

# ---------------------------------------------------------------------------
# 6. Triage that needs a person stops the one-shot path
# ---------------------------------------------------------------------------
def overlapping(request):
    answer = testkit.triage_answer(request)
    answer["documents"][0]["last_sheet"] = 1          # leaves sheet 2 unclaimed, twice
    return answer


try:
    run(marlowe(0), StubModel({"record_triage": overlapping}))
    check(False, "needs_attention must stop the one-shot path")
except NeedsConfirmation as exc:
    check("confirm screen" in str(exc), "needs attention: the caller is sent to the confirm screen")
two_matters = lambda r: {"documents": [dict(d, principal=f"Person {i}") for i, d in enumerate(testkit.triage_answer(r)["documents"])]}
try:
    run(marlowe(0, 1), StubModel({"record_triage": two_matters}))
    check(False, "two matters must stop the one-shot path")
except NeedsConfirmation as exc:
    check("more than one matter" in str(exc), "possible multiple matters: the caller is asked to choose")

# ---------------------------------------------------------------------------
# 7. Load errors and the plan round trip
# ---------------------------------------------------------------------------
def raises_kind(fn, kind):
    try:
        fn()
    except ExtractionError as e:
        return e.kind == kind
    return False


check(raises_kind(lambda: load_files([UploadedFile("scan.pdf", (FIX / "scan_like_blank.pdf").read_bytes())], SETTINGS),
                  "no_text_layer"), "a scan is refused before any model call")
check(raises_kind(lambda: load_files([UploadedFile("x.docx", b"PK")], SETTINGS), "unsupported"), "unsupported type refused")
loaded = load_files([txt("a.pdf.txt", "May 1, 2001"), txt("a.pdf.txt", "May 2, 2001")], SETTINGS)
check([f.file_id for f in loaded.files] == ["a.pdf.txt", "a.pdf (2).txt"], "duplicate file names get unique ids")
gappy = UploadedFile("gappy.txt", ("Page one has plenty of words to be readable for sure.\fx\fPage three also "
                                   "has plenty of readable words in it.").encode())
check(any("sheet(s) 2 have no extractable text" in w for w in load_files([gappy], SETTINGS).warnings),
      "a sheet with no text is reported by sheet number")
try:
    load_files([], SETTINGS)
    check(False, "no files must fail")
except ValueError:
    check(True, "no files is refused")

install_stub(service)
loaded = load_files(marlowe(0, 3, 4), SETTINGS)
outcome = triage_packet(loaded, SETTINGS)
raw = json.loads(json.dumps(outcome.plan.to_dict()))
sheets = {f.file_id: f.sheets for f in loaded.files}
plan, problems = validate_plan(raw, sheets)
check(problems == [] and [d.id for d in plan.documents] == ["d1", "d2", "d3"], "a plan survives the JSON round trip")
check(all("text" not in json.dumps(d) for d in raw["documents"]) and raw["documents"][0]["labels"][0]["label"] == "1",
      "the plan carries labels, never page text")


def problems_for(mutate):
    bad = json.loads(json.dumps(raw))
    mutate(bad)
    return validate_plan(bad, sheets)[1]


file0 = raw["documents"][0]["file_id"]
cases = [
    (lambda p: p["documents"][0].update(last_sheet=1), "1 sheet(s) are in no document"),
    (lambda p: p["documents"][1].update(file_id=file0, first_sheet=1, last_sheet=2), "in more than one document"),
    (lambda p: p["documents"][0].update(last_sheet=9), "sheets must be whole numbers within 1-2"),
    (lambda p: p["documents"][0].update(doc_type="novel"), "doc_type is not a known type"),
    (lambda p: p["documents"][0].update(treatment="shred"), "treatment must be one of"),
    (lambda p: p["documents"][0].update(date="12/03/1998"), "date must be YYYY-MM-DD or null"),
    (lambda p: p["documents"][0].update(amends="d9"), "amends names a document that is not in the plan"),
    (lambda p: p["documents"][0].update(amends="d1"), "amends must be another document's id"),
    (lambda p: p["documents"][1].update(id="d1"), "id is missing, too long or repeated"),
    (lambda p: p["files"].pop(), "the plan's files do not match"),
    (lambda p: p.update(version=99), "plan version must be 1"),
    (lambda p: p["documents"][0].update(excluded="yes"), "excluded must be true or false"),
]
for mutate, fragment in cases:
    found = problems_for(mutate)
    check(any(fragment in p for p in found), f"plan validation: {fragment} ({found})")
check(validate_plan("nope", sheets)[1] == ["the plan must be a JSON object"], "a non-object plan is refused")
check(PLAN_VERSION == 1, "plan version 1")

edited = json.loads(json.dumps(raw))
edited["documents"][2]["excluded"] = True                 # leave out the 2016 amendment
edited["documents"][0]["treatment"] = "log_only"          # and log the 1998 trust only
plan, problems = validate_plan(edited, sheets)
result = annotate_plan(plan, loaded.pages_by_file, SETTINGS)
wb = load_workbook(io.BytesIO(result.xlsx))
rows = packet_rows(wb)
check(problems == [] and [r["treatment"] for r in rows] == ["log_only", "annotate", "excluded"],
      "confirm-screen edits reach the packet map")
check(sheet_ok(wb[wb.sheetnames[2]], 0) and len(wb.sheetnames) == 3, "the edited plan is a restatement alone (n = 0)")

# ---------------------------------------------------------------------------
# 8. The command-line tool: same functions, auto-confirm
# ---------------------------------------------------------------------------
import cli as cli_module

stub = install_stub(service)
out_dir = tempfile.mkdtemp()
cli_module.OUTPUT_DIR = out_dir
cli_module.DOC_NAME = "CLI Test"
cli_module.FILES = [str(p) for p in sorted(FIX.glob("0*.pdf"))]
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    cli_module.main()
check((Path(out_dir) / "CLI_Test_annotation.xlsx").exists(), "cli: the workbook is written to OUTPUT_DIR")
check("[document] d4" in captured.getvalue() and len(stub.calls("record_triage")) == 1, "cli: prints the document map")
check("UNIVERSITY" not in captured.getvalue(), "cli: prints no document text")
cli_module.FILES = [str(FIX / "scan_like_blank.pdf")]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        cli_module.main()
    check(False, "cli: a scan must stop the run")
except ExtractionError as exc:
    check("no text layer" in str(exc), "cli: a scanned PDF stops the run with the explanation")
cli_module.FILES = [str(FIX / "00_marlowe_trust_original.pdf")]
install_stub(service, StubModel({"record_triage": overlapping}))
try:
    with contextlib.redirect_stdout(io.StringIO()):
        cli_module.main()
    check(False, "cli: needs attention must stop")
except SystemExit as exc:
    check("confirm screen" in str(exc), "cli: triage that needs a person stops with a pointer to the confirm screen")

# ---------------------------------------------------------------------------
# 9. No document text in progress or warnings; nothing written to disk
# ---------------------------------------------------------------------------
check(len(SURFACES) > 10 and not any(MARKER in s for s in SURFACES), "progress and warnings carry no document text")
before = set(Path(".").rglob("*.xlsx"))
run(marlowe(0))
check(set(Path(".").rglob("*.xlsx")) == before, "a run writes no files (the result travels in memory)")

print(f"\nALL {passed} CHECKS PASSED")
