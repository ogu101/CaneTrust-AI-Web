"""
API-free tests for page reading and the web API.

    python test_web.py

Every model call goes to testkit.StubModel (installed into service), so no API key or network
is needed and the real pipeline runs. The PDFs are the FICTIONAL Marlowe packet in fixtures/.
"""

import base64
import contextlib
import io
import json
import os
import time
from pathlib import Path

os.environ["CANETRUST_DISABLE_DOTENV"] = "1"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
for name in ("VERCEL", "CANETRUST_ACCESS_CODE", "CANETRUST_MAX_UPLOAD_MB", "CANETRUST_DEADLINE_SECONDS"):
    os.environ.pop(name, None)

import anthropic
import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader, PdfWriter

import testkit
from testkit import StubModel, install_stub

import service
from api import index as api_index
from page_reader import ExtractionError, read_pages
from workbook import PACKET_MAP_SHEET

FIX = Path(__file__).parent / "fixtures"
MARKER = "Zephyrquill"
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


@contextlib.contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def pdf(name):
    return (FIX / name).read_bytes()


def blank_pdf(pages=1):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def raises_kind(fn, kind):
    try:
        fn()
    except ExtractionError as e:
        return e.kind == kind, str(e)
    return False, "did not raise"


# ---------------------------------------------------------------------------
# 1. Reading pages
# ---------------------------------------------------------------------------
TRUST = pdf("00_marlowe_trust_original.pdf")

pages = read_pages("trust.pdf", "trust.pdf", TRUST)
check(len(pages) == 2 and [p.position for p in pages] == [1, 2], "a text PDF gives one record per page")
check("4.3 Distribution of the Residue" in pages[0].text and "4.3 Distribution of the Residue" not in pages[1].text,
      "page records follow the real page boundary (4.3 starts on page 1)")
check("UNIVERSITY OF MIAMI" in pages[0].text and "[PDF page" not in pages[0].text,
      "the document's own text survives, with no PDF position markers")

ok, msg = raises_kind(lambda: read_pages("s", "scan.pdf", blank_pdf(3)), "no_text_layer")
check(ok and "scan" in msg and "scan.pdf" in msg, "a PDF with no text layer is refused, by name, as a scan")

writer = PdfWriter()
for page in PdfReader(io.BytesIO(TRUST)).pages:
    writer.add_page(page)
writer.encrypt("secret")
locked = io.BytesIO()
writer.write(locked)
check(raises_kind(lambda: read_pages("l", "locked.pdf", locked.getvalue()), "encrypted")[0], "a password-protected PDF is refused")
check(raises_kind(lambda: read_pages("f", "fake.pdf", b"hello"), "invalid")[0], "a non-PDF named .pdf is refused")
check(raises_kind(lambda: read_pages("b", "brief.docx", b"PK..."), "unsupported")[0], "an unsupported type is refused")
check(raises_kind(lambda: read_pages("e", "e.txt", b"  \n"), "empty")[0], "an empty .txt is refused")
check(raises_kind(lambda: read_pages("t", "t.pdf", TRUST, max_pages=1), "too_long")[0], "a PDF over the page limit is refused")
check(read_pages("q", "q.txt", "“quoted”".encode("cp1252"))[0].text == "“quoted”", "cp1252 text decodes")
check(read_pages("u", "u.txt", "café".encode("utf-8"))[0].text == "café", "utf-8 text decodes")

# ---------------------------------------------------------------------------
# 2. The API, with the model stubbed
# ---------------------------------------------------------------------------
client = TestClient(api_index.app)
STUB = install_stub(service)

AMENDMENT_FILES = ["01_marlowe_first_amendment.pdf", "02_marlowe_second_amendment.pdf",
                   "03_marlowe_third_amendment_and_restatement.pdf", "04_marlowe_fourth_amendment.pdf"]


def post(trust=("00_marlowe_trust_original.pdf", TRUST), amendments=(), data=None, headers=None):
    files = [("trust", (trust[0], trust[1], "application/pdf"))]
    for fname in amendments:
        files.append(("amendment_files", (fname, pdf(fname), "application/pdf")))
    meta = [{"number": i + 1, "label": ""} for i in range(len(amendments))]
    form = {"amendments_meta": json.dumps(meta), **(data or {})}
    return client.post("/api/annotate", files=files, data=form,
                       headers={"Accept": "application/x-ndjson", **(headers or {})})


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def settle(timeout=10.0):
    """Wait for worker threads left running by a stream that ended early (the deadline test),
    so they cannot answer into the next test's stub."""
    import threading
    end = time.monotonic() + timeout
    while time.monotonic() < end and any(t.daemon and t.is_alive() and t.name.startswith("Thread-")
                                         for t in threading.enumerate()):
        time.sleep(0.05)


def stub(answers=None, pause=0.0):
    global STUB
    STUB = install_stub(service, StubModel(answers, pause))
    return STUB


# -- health
h = client.get("/api/health")
check(h.status_code == 200 and h.json()["ok"] and h.json()["assets_present"], "health: assets are bundled")
check("test-key-not-used" not in h.text and h.json()["api_key_set"] is True, "health reports the key is set, never the key")

# -- a trust and four amendments, uploaded OUT of order
stub()
r = post(amendments=[AMENDMENT_FILES[3], AMENDMENT_FILES[1], AMENDMENT_FILES[0], AMENDMENT_FILES[2]])
ev = events(r)
check(r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson"), "success streams NDJSON")
check(ev[0] == {"type": "progress", "message": "Documents accepted."}, "first event: documents accepted")
check(any(e["type"] == "progress" and "Found 5 document(s)" in e["message"] for e in ev), "progress reports triage")
check(any(e["type"] == "progress" and "1 chain(s) to annotate" in e["message"] for e in ev), "progress reports the chains")
done = ev[-1]
check(done["type"] == "done" and done["route"] == "unified", "last event is 'done', on the one pipeline")
check(done["filename"] == "00_marlowe_trust_original_annotation.xlsx", "output filename is derived from the first file")
check(done["chains"] and done["chains"][0]["result"] == "annotated", "the done event summarizes the chains")
calls = STUB.calls("analyze_documents")
check(len(calls) == 1 and "[Page 1]" in calls[0]["messages"][0]["content"]
      and "UNIVERSITY OF MIAMI" in calls[0]["messages"][0]["content"],
      "the model receives page text with printed labels")
wb = load_workbook(io.BytesIO(base64.b64decode(done["xlsx_base64"])))
ws = wb[wb.sheetnames[2]]
check(ws["G11"].value.startswith("Amendment 1: 04 marlowe fourth amendment (2016-10-17)"),
      "the amendment column follows the dates in the documents, not upload order")
check(ws.column_dimensions["H"].hidden and ws.column_dimensions["N"].hidden and not ws.column_dimensions["G"].hidden,
      "unused amendment columns are hidden")
check(PACKET_MAP_SHEET in wb.sheetnames, "the workbook has a packet map")

# -- trust only
stub()
ev = events(post())
check(ev[-1]["type"] == "done" and len(STUB.calls("analyze_documents")) == 1, "a trust alone is annotated (n = 0)")
wb = load_workbook(io.BytesIO(base64.b64decode(ev[-1]["xlsx_base64"])))
check(wb[wb.sheetnames[2]].column_dimensions["P"].hidden, "n = 0: the operative block is hidden")

# -- explicit names are honoured and the filename is made safe
r = post(data={"doc_name": "Marlowe / Trust: 1998?"})
check(events(r)[-1]["filename"] == "Marlowe__Trust_1998_annotation.xlsx", "unsafe characters are stripped from the download name")
r = post(data={"trust_label": "Marlowe Trust (1998)"})
check(events(r)[-1]["filename"] == "Marlowe_Trust_(1998)_annotation.xlsx", "the trust label names the matter when no doc_name is given")

# -- plain .txt uploads work too
r = post(trust=("trust.txt", b"The trust text, signed March 1, 2001. UNIVERSITY OF MIAMI receives the residue."))
check(events(r)[-1]["type"] == "done", ".txt uploads are accepted")

# -- access control
with env(CANETRUST_ACCESS_CODE="sesame"):
    r = post()
    check(r.status_code == 401 and r.json()["code"] == "bad_access_code", "no access code -> 401")
    check(post(data={"access_code": "wrong"}).status_code == 401, "wrong access code -> 401")
    check(events(post(data={"access_code": "sesame"}))[-1]["type"] == "done", "right code in the form -> allowed")
    check(events(post(headers={"X-Access-Code": "sesame"}))[-1]["type"] == "done", "right code in a header -> allowed")
with env(VERCEL="1"):
    r = post()
    check(r.status_code == 503 and r.json()["code"] == "not_configured", "deployed with no access code configured: refuses to run open")
    check(client.get("/api/health").json()["access_code_required"] is False, "health shows the gate is not configured")

# -- validation errors: proper HTTP statuses, before anything streams
calls_before = len(STUB.requests)
r = post(trust=("scan.pdf", blank_pdf(2)))
check(r.status_code == 422 and r.json()["code"] == "no_text_layer" and "scan.pdf" in r.json()["error"], "a scanned file -> 422, naming the file")
r = post(amendments=[AMENDMENT_FILES[0]], trust=("scan.pdf", blank_pdf(1)))
check(r.status_code == 422, "a scan among several files -> 422")
r = post(trust=("notes.docx", b"PK\x03\x04"))
check(r.status_code == 415 and r.json()["code"] == "unsupported", "an unsupported file type -> 415")
r = client.post("/api/annotate", data={})
check(r.status_code == 422, "no file at all -> rejected")
with env(CANETRUST_MAX_UPLOAD_MB="0.001"):
    r = post()
    check(r.status_code == 413 and r.json()["code"] == "too_large", "an oversized upload -> 413")
with env(CANETRUST_MAX_CHARS="500"):
    r = post()
    check(r.status_code == 400 and "character" in r.json()["error"], "too much text for one packet -> 400")
check(len(STUB.requests) == calls_before, "no model call is made for a refused upload")

# -- model failures arrive as a final 'error' event, with safe messages
request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
SECRET = "boom-SECRET-document-text"
bad_answer = testkit.chain_annotation(()).model_dump(mode="json")
bad_answer["bequest_language"]["location"]["original"]["status"] = SECRET
cases = [
    (anthropic.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None), "rate_limited"),
    (anthropic.AuthenticationError("bad key sk-ant-xyz", response=httpx.Response(401, request=request), body=None), "model_auth"),
    (anthropic.APIConnectionError(request=request), "model_unreachable"),
    (anthropic.InternalServerError("oops", response=httpx.Response(500, request=request), body=None), "model_error"),
    (RuntimeError("Output was cut off at max_tokens=8000"), "run_failed"),
    (ValueError(SECRET), "internal"),
    (bad_answer, "bad_model_output"),
]
stderr = io.StringIO()
for exc, code in cases:
    stub({"analyze_documents": exc})
    with contextlib.redirect_stderr(stderr):
        r = post()
    ev = events(r)
    check(r.status_code == 200 and ev[-1]["type"] == "error" and ev[-1]["code"] == code,
          f"model failure {code} -> error event {code!r}")
    shown = ev[-1]["message"]
    check(SECRET not in shown and "sk-ant" not in shown, f"{code}: the user-facing message leaks nothing")
    if code == "run_failed":
        check("Output was cut off" in shown, "a crafted RuntimeError message is passed through to the user")
logged = stderr.getvalue()
check("[error] ValueError: (message withheld)" in logged and SECRET not in logged and "sk-ant" not in logged,
      "server logs name the error class but never the message of an unexpected exception")
check("[error] RuntimeError: Output was cut off" in logged, "messages we wrote ourselves are logged")

stub({"record_triage": lambda req: dict(testkit.triage_answer(req), documents=[
    dict(d, last_sheet=1) for d in testkit.triage_answer(req)["documents"]])})
ev = events(post())
check(ev[-1]["type"] == "error" and ev[-1]["code"] == "needs_confirmation" and "confirm screen" in ev[-1]["message"],
      "triage that needs a person: the one-shot request points to the confirm screen")

stub()
with env(ANTHROPIC_API_KEY=None):
    ev = events(post())
    check(ev[-1]["type"] == "error" and "ANTHROPIC_API_KEY" in ev[-1]["message"], "a missing API key is reported plainly")

# -- long runs: heartbeats keep the stream alive, and a deadline ends it cleanly
api_index.HEARTBEAT_SECONDS = 0.15
stub(pause=0.25)
ev = events(post())
beats = [e for e in ev if e["type"] == "heartbeat"]
check(len(beats) >= 2 and ev[-1]["type"] == "done", "a slow run emits heartbeats, then completes")
with env(CANETRUST_DEADLINE_SECONDS="1"):
    stub(pause=1.5)
    ev = events(post())
    check(ev[-1]["type"] == "error" and ev[-1]["code"] == "deadline", "past the deadline the stream ends with a clear error")
api_index.HEARTBEAT_SECONDS = 10
settle()
stub()

# ---------------------------------------------------------------------------
# 2b. Classic mode: the original contract (file/label/status in, workbook bytes out)
# ---------------------------------------------------------------------------
def classic(file=("t.pdf", TRUST), data=None, headers=None):
    return client.post("/api/annotate", files=[("file", (file[0], file[1], "application/pdf"))],
                       data=data or {}, headers=headers or {})


r = classic(data={"label": "Trust document", "status": "OPERATIVE"})
check(r.status_code == 200 and r.headers["content-type"] == api_index.XLSX, "classic: the workbook itself comes back")
check(r.headers["content-disposition"].startswith('attachment; filename="Trust_document_annotation.xlsx"'),
      "classic: the label names the download")
check(r.headers["x-canetrust-route"] == "unified", "classic: the route is reported in a header")
wb = load_workbook(io.BytesIO(r.content))
check(wb.sheetnames[:2] == ["Instructions", PACKET_MAP_SHEET] and len(wb.sheetnames) == 3, "classic: the bytes are the unified workbook")
check(classic(data={"status": "IN FORCE"}).status_code == 200, "classic: a custom status is still accepted")
r = classic()
check(r.headers["content-disposition"].startswith('attachment; filename="t_annotation.xlsx"'), "classic: name defaults to the file name")

r = classic(file=("Trüst – 1998.pdf", TRUST))
check(r.status_code == 200 and "filename*=UTF-8''" in r.headers["content-disposition"]
      and r.headers["content-disposition"].isascii(), "classic: a non-ASCII file name is safe in the header")

r = client.post("/api/annotate", data={"label": "x"})
check(r.status_code == 422 and r.json()["code"] == "missing_file", "classic: no file -> 422 as JSON")
r = classic(file=("scan.pdf", blank_pdf(1)))
check(r.status_code == 422 and r.json()["code"] == "no_text_layer", "classic: a scan -> 422 with the explanation")

classic_failures = [
    (anthropic.RateLimitError("slow", response=httpx.Response(429, request=request), body=None), 429, "rate_limited"),
    (anthropic.AuthenticationError("bad key sk-ant-q", response=httpx.Response(401, request=request), body=None), 502, "model_auth"),
    (RuntimeError("Output was cut off at max_tokens=8000"), 500, "run_failed"),
    (ValueError("boom-SECRET-document-text"), 500, "internal"),
]
sink = io.StringIO()
for exc, status_code, code in classic_failures:
    stub({"analyze_documents": exc})
    with contextlib.redirect_stderr(sink):
        r = classic()
    check(r.status_code == status_code and r.json()["code"] == code, f"classic: {code} -> HTTP {status_code}")
    check("SECRET" not in r.text and "sk-ant" not in r.text, f"classic {code}: nothing sensitive in the response")
stub({"record_triage": lambda req: dict(testkit.triage_answer(req), documents=[
    dict(d, last_sheet=1) for d in testkit.triage_answer(req)["documents"]])})
r = classic()
check(r.status_code == 409 and r.json()["code"] == "needs_confirmation" and "confirm screen" in r.json()["error"],
      "classic: triage that needs a person -> 409 pointing to the confirm screen")
stub()

with env(CANETRUST_ACCESS_CODE="sesame"):
    check(classic().status_code == 401, "classic: the access code is enforced here too")
    check(classic(headers={"X-Access-Code": "sesame"}).status_code == 200, "classic: the code is accepted in a header")
with env(VERCEL="1"):
    check(classic().status_code == 503, "classic: a deployment with no code refuses to run open")
with env(VERCEL="1", CANETRUST_ALLOW_OPEN="1"):
    check(classic().status_code == 200, "CANETRUST_ALLOW_OPEN lets a code-less deployment run (explicit opt-in)")
    check(client.get("/api/health").json()["open_access_allowed"] is True, "health reports that open access is on")
check(client.get("/api/health").json()["open_access_allowed"] is False, "open access is off by default")

# ---------------------------------------------------------------------------
# 2c. The two-step flow: /api/triage, then /api/annotate with a confirmed plan
# ---------------------------------------------------------------------------
ALL = ["00_marlowe_trust_original.pdf", *AMENDMENT_FILES]
settle()


def triage_post(names=ALL, extra=None, data=None, headers=None):
    files = [("files", (n, pdf(n), "application/pdf")) for n in names] + list(extra or [])
    return client.post("/api/triage", files=files, data=data or {}, headers=headers or {})


def annotate_json(body, headers=None):
    return client.post("/api/annotate", json=body, headers=headers or {})


stub()
r = triage_post(list(reversed(ALL)))
ev = events(r)
check(r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson"), "triage streams NDJSON")
check(ev[0] == {"type": "progress", "message": "Files accepted."} and ev[-1]["type"] == "triaged", "triage: accepted ... triaged")
TRIAGED = ev[-1]
plan_json, pages_json = TRIAGED["plan"], TRIAGED["pages"]
check(plan_json["version"] == 1 and len(plan_json["documents"]) == 5 and [f["file_id"] for f in plan_json["files"]] == list(reversed(ALL)),
      "the plan lists files in upload order and every document")
check(set(pages_json) == set(ALL) and len(pages_json[ALL[0]]) == 2 and "UNIVERSITY OF MIAMI" in pages_json[ALL[0]][0],
      "the page texts come back for the browser to hold")
check(TRIAGED["skip_confirm"] is False, "five documents: the confirm screen is not skipped")
check(len(STUB.calls("record_triage")) == 1 and not STUB.calls("analyze_documents"), "triage makes no annotation call")
one = events(triage_post([ALL[0]]))[-1]
check(one["skip_confirm"] is True, "one confident, unflagged document: the confirm screen may be skipped")
check(events(triage_post([ALL[0]], data={"doc_name": "My Matter"}))[-1]["plan"]["name"] == "My Matter", "doc_name names the matter")

calls_before = len(STUB.requests)
r = triage_post([], extra=[("files", ("scan.pdf", blank_pdf(2), "application/pdf"))])
check(r.status_code == 422 and r.json()["code"] == "no_text_layer" and "scan.pdf" in r.json()["error"],
      "triage: a scan is refused before streaming, by name")
r = triage_post([], extra=[("files", ("notes.docx", b"PK\x03\x04", "application/octet-stream"))])
check(r.status_code == 415, "triage: an unsupported type -> 415")
check(client.post("/api/triage", data={}).status_code == 422, "triage: no files -> 422")
with env(CANETRUST_MAX_UPLOAD_MB="0.001"):
    r = triage_post()
    check(r.status_code == 413 and r.json()["code"] == "too_large" and "MB limit" in r.json()["error"], "triage: over the upload limit -> 413")
with env(CANETRUST_MAX_STATE_MB="0.001"):
    r = triage_post()
    check(r.status_code == 413 and r.json()["code"] == "state_too_large" and "browser can keep" in r.json()["error"],
          "triage: more page text than the browser may hold -> 413 with a clear message")
check(len(STUB.requests) == calls_before, "no model call for a refused triage request")
with env(CANETRUST_ACCESS_CODE="sesame"):
    check(triage_post().status_code == 401, "triage: the access code is enforced")
    check(triage_post(data={"access_code": "sesame"}).status_code == 200, "triage: the right code is accepted")
with env(VERCEL="1"):
    check(triage_post().status_code == 503, "triage: a deployment with no code refuses to run open")
stub({"record_triage": anthropic.RateLimitError("slow", response=httpx.Response(429, request=request), body=None)})
with contextlib.redirect_stderr(io.StringIO()):
    ev = events(triage_post())
check(ev[-1]["type"] == "error" and ev[-1]["code"] == "rate_limited", "triage: a model failure ends the stream with an error event")
api_index.HEARTBEAT_SECONDS = 0.1
stub(pause=0.35)
ev = events(triage_post([ALL[0]]))
check(any(e["type"] == "heartbeat" for e in ev) and ev[-1]["type"] == "triaged", "triage: heartbeats while the model works")
api_index.HEARTBEAT_SECONDS = 10

# -- annotate a confirmed plan
stub()
confirmed = json.loads(json.dumps(plan_json))
by_file = {d["file_id"]: d for d in confirmed["documents"]}
by_file[ALL[4]]["excluded"] = True
by_file[ALL[4]]["note"] = "excluded on review"
confirmed["edits"] = [{"document": by_file[ALL[4]]["id"], "field": "excluded", "from": "false", "to": "true"}]
for d in confirmed["documents"]:
    d["labels"] = [{"sheet": 1, "label": "999", "source": "printed"}]     # the client cannot set labels
r = annotate_json({"plan": confirmed, "pages": pages_json})
ev = events(r)
check(r.status_code == 200 and ev[0] == {"type": "progress", "message": "Plan accepted."} and ev[-1]["type"] == "done",
      "annotate: plan accepted ... done")
msg = STUB.calls("analyze_documents")[0]["messages"][0]["content"]
check("[Page 1]" in msg and "[Page 999]" not in msg, "labels are recomputed on the server, never taken from the client")
check("04 marlowe fourth" not in msg.split("Instruments in this chain")[1].split("\n\n")[0],
      "an excluded document is not annotated")
wb = load_workbook(io.BytesIO(base64.b64decode(ev[-1]["xlsx_base64"])))
pm_values = [c.value for row in wb[PACKET_MAP_SHEET].iter_rows() for c in row if c.value]
check("excluded" in pm_values and "excluded on review" in " ".join(map(str, pm_values)),
      "the packet map shows the exclusion and the reviewer's note")
check(any(str(v).startswith("Corrected on the confirm screen:") for v in pm_values),
      "confirm-screen corrections are kept in the workbook as an audit record")

bad = json.loads(json.dumps(plan_json))
next(d for d in bad["documents"] if d["file_id"] == ALL[0])["last_sheet"] = 1
r = annotate_json({"plan": bad, "pages": pages_json})
check(r.status_code == 422 and r.json()["code"] == "invalid_plan" and any("in no document" in p for p in r.json()["problems"]),
      "annotate: a plan that leaves a sheet uncovered is refused, with the problems listed")
bad = json.loads(json.dumps(plan_json))
bad["documents"][0]["treatment"] = "shred"
check(annotate_json({"plan": bad, "pages": pages_json}).status_code == 422, "annotate: an unknown treatment is refused")
fewer = dict(pages_json)
fewer[ALL[0]] = pages_json[ALL[0]][:1]
r = annotate_json({"plan": plan_json, "pages": fewer})
check(r.status_code == 422 and any("sheets must be whole numbers within 1-1" in p for p in r.json()["problems"]),
      "annotate: ranges are checked against the pages actually sent")
check(annotate_json({"plan": plan_json, "pages": "x"}).json()["code"] == "bad_pages", "annotate: pages must be a map")
r = client.post("/api/annotate", content=b"{not json", headers={"content-type": "application/json"})
check(r.status_code == 400 and r.json()["code"] == "bad_request", "annotate: malformed JSON -> 400")
with env(CANETRUST_MAX_STATE_MB="0.001"):
    r = annotate_json({"plan": plan_json, "pages": pages_json})
    check(r.status_code == 413 and r.json()["code"] == "state_too_large", "annotate: state over the cap is refused")
with env(CANETRUST_ACCESS_CODE="sesame"):
    check(annotate_json({"plan": plan_json, "pages": pages_json}).status_code == 401, "annotate: the access code is enforced")
    ev = events(annotate_json({"plan": plan_json, "pages": pages_json, "access_code": "sesame"}))
    check(ev[-1]["type"] == "done", "annotate: the code in the JSON body is accepted")

many_pages = {f"a{i}.txt": [f"Amendment {i} text, signed January {i}, 2001."] for i in range(1, 11)}
many_plan = {"version": 1, "name": "Many", "files": [{"file_id": f, "filename": f} for f in many_pages], "documents": [
    {"id": f"d{i}", "file_id": f"a{i}.txt", "first_sheet": 1, "last_sheet": 1,
     "doc_type": "trust" if i == 1 else "amendment", "treatment": "annotate", "principal": "P",
     "date": f"2001-01-{i:02d}"} for i in range(1, 11)]}
r = annotate_json({"plan": many_plan, "pages": many_pages})
check(r.status_code == 422 and any("room for 8" in p for p in r.json()["problems"]),
      "annotate: a chain with more amendments than the workbook holds is refused before any call")

stub({"analyze_documents": anthropic.RateLimitError("slow", response=httpx.Response(429, request=request), body=None)})
with contextlib.redirect_stderr(io.StringIO()):
    ev = events(annotate_json({"plan": plan_json, "pages": pages_json}))
check(ev[-1]["type"] == "error" and ev[-1]["code"] == "rate_limited", "annotate: a model failure ends with an error event")
stub()
check(client.get("/api/health").json()["max_state_mb"] == 3.0, "health reports the state cap")

# -- nothing is written to the working directory by a run
before = set(Path(".").rglob("*.xlsx"))
events(post())
check(set(Path(".").rglob("*.xlsx")) == before, "a run writes no files (the result travels in memory)")

print(f"\nALL {passed} CHECKS PASSED")
