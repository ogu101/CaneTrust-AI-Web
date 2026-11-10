"""
API-free tests for triage (docs/DESIGN.md, milestone 3).

    .venv/bin/python test_triage.py

Pages are INVENTED from the structure of fixtures/gay/expected_map.json (ranges, types, dates,
label rules) with generic words: no names, addresses or phrases from the real documents. The
model is a stub that answers what the map implies, or a deliberately broken variant of it.
"""

import contextlib
import io
import json
import logging
import os
import re
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import testkit                                   # blocks real model calls; disables .env
from testkit import strict_schema_problems

from page_reader import PageRecord
from settings import load_settings
from triage import TriageError, build_request, load_treatments, normalize_principal, triage
from triage_previews import build_previews, estimate_tokens, page_features
from triage_schema import DOC_TYPES, TOOL_NAME, TriageAnswer, tool_definition

ROOT = Path(__file__).parent
MAP = json.loads((ROOT / "fixtures" / "gay" / "expected_map.json").read_text(encoding="utf-8"))
SETTINGS = load_settings()
passed = 0

# Planted in every invented page and in every string the stub model returns. It must never
# reach a warning, flag, problem, error or log line.
MARKER = "Zephyrquill"
PRINCIPAL = f"Ambrose Q. {MARKER}"


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


# ---------------------------------------------------------------------------
# Invented pages from the map's structure
# ---------------------------------------------------------------------------
TITLES = {
    "restated_trust": "Specimen Amended and Restated Trust Instrument",
    "accounting": "Specimen Statement of Account",
    "receipt_of_notice": "Specimen Acknowledgment Form",
    "letter": "Specimen Correspondence",
    "letters_of_administration": "Specimen Appointment Order",
    "will": "Specimen Testamentary Instrument",
    "codicil": "Specimen Supplement to Testamentary Instrument",
    "notice_of_administration": "Specimen Administration Notice",
    "notice_to_creditors": "Specimen Notice for Claimants",
}
BODY = [
    f"The {MARKER} provisions on this sheet are invented filler for tests only.",
    "Each paragraph here says nothing about any real person or real property.",
    "The parties named in this specimen are placeholders and carry no meaning.",
    "This sentence exists so the sheet has enough words to look like a page.",
    "Further filler text continues here with ordinary words and no numbers.",
    "Nothing in this specimen is drawn from any actual estate planning record.",
]
FIRM = ["Specimen Law Office LLP", "Invented Building, Example Suite"]
DOC_NUMBERS = {"trust": "48210001", "will": "48210002", "codicil": "48210003"}
FOOTER_DOCS = {"restated_trust", "will", "codicil"}


def expected_labels():
    """{(file, sheet): (label, source)} for every asserted label rule."""
    out = {}
    for d in MAP["documents"]:
        for rule in d["label_rules"]:
            if not rule.get("assert", True):
                continue
            first, last = rule["sheets"]
            labels = rule.get("labels") or [str(rule["labels_from"] + k) for k in range(last - first + 1)]
            for sheet, label in zip(range(first, last + 1), labels):
                out[(d["file"], sheet)] = (label, rule["source"])
    return out


LABELS = expected_labels()


def month_day_year(iso):
    day = date.fromisoformat(iso)
    return f"{day:%B} {day.day}, {day.year}"


def invented_page(doc, sheet, doc_numbers):
    """One invented page for sheet `sheet` of map document `doc`."""
    label, source = LABELS.get((doc["file"], sheet), (None, None))
    printed = label if source == "printed" else None
    header = [f"{MARKER} specimen sheet", "Invented heading line"]
    body = list(BODY)
    if sheet == doc["first"]:
        header = [TITLES[doc["type"]], f"Prepared for {PRINCIPAL}"]
        if doc["date"]:
            body.insert(2, f"Dated {month_day_year(doc['date'])} for this specimen.")
        if doc["type"] == "restated_trust":
            body.insert(3, "One share passes to the University of Miami under this specimen.")
    if doc["type"] == "letter" and printed:
        header = [f"{TITLES['letter']} - Page {printed}", f"{MARKER} continued"]
    footer = []
    if doc["type"] in FOOTER_DOCS:
        footer = ([printed] if printed else []) + FIRM + [doc_numbers.get(doc["id"], "48219999")]
    elif doc["type"] == "notice_of_administration" and printed:
        footer = [f"Form No. X-1 - {printed} of 2"]
    return "\n".join(header + body + footer)


def invented_packet(doc_numbers=None):
    numbers = {**DOC_NUMBERS, **(doc_numbers or {})}
    pages = {name: [None] * info["sheets"] for name, info in MAP["files"].items()}
    for d in MAP["documents"]:
        for sheet in range(d["first"], d["last"] + 1):
            pages[d["file"]][sheet - 1] = PageRecord(d["file"], sheet, invented_page(d, sheet, numbers))
    assert all(all(p is not None for p in ps) for ps in pages.values())
    return pages


# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------
_HEADER_RE = re.compile(r"^\[(.+) sheet (\d+)\]$", re.M)


def shown_sheets(request):
    message = request["messages"][0]["content"]
    shown = {}
    for file_id, sheet in _HEADER_RE.findall(message.split("\nYour previous answer")[0]):
        shown.setdefault(file_id, set()).add(int(sheet))
    return shown


def answer_doc(d, first=None, last=None, **overrides):
    doc = {
        "file_id": d["file"], "first_sheet": first or d["first"], "last_sheet": last or d["last"],
        "doc_type": d["type"], "title": f"{MARKER} title", "principal": PRINCIPAL,
        "date": d["date"] or "unknown",
        "refers_to": [{"what": f"{MARKER} instrument", "date": r["date"]} for r in d.get("refers_to", [])],
        "revokes": [],
        "appears_to_gift_to_university": "yes" if d["type"] == "restated_trust" else "no",
        "is_blank_form": d["id"] == "receipt_blank", "proposed_treatment": d["treatment"],
        "treatment_reason": f"{MARKER} reason", "confidence": "high", "evidence": [f"{MARKER} cue"],
    }
    doc.update(overrides)
    return doc


def truth_answer(request, truth=None, overrides=None):
    """The answer implied by `truth` (map-style documents), clipped to the sheets shown."""
    shown = shown_sheets(request)
    docs = []
    for d in truth or MAP["documents"]:
        sheets = sorted(s for s in shown.get(d["file"], ()) if d["first"] <= s <= d["last"])
        if sheets:
            docs.append(answer_doc(d, sheets[0], sheets[-1], **(overrides or {}).get(d["id"], {})))
    return {"documents": docs}


class StubClient:
    """Answers with answers[i](request) on call i (the last one repeats)."""

    def __init__(self, *answers):
        self.answers = answers
        self.requests = []
        self.messages = self

    def stream(self, **request):
        self.requests.append(request)
        raw = self.answers[min(len(self.requests), len(self.answers)) - 1](request)
        content = [] if raw is None else [SimpleNamespace(type="tool_use", name=TOOL_NAME, input=raw)]
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=800,
                                cache_creation_input_tokens=0)
        message = SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)

        @contextlib.contextmanager
        def ctx():
            yield SimpleNamespace(get_final_message=lambda: message)
        return ctx()


# Everything that must stay free of document text, collected across the whole run.
SURFACES: list[str] = []
_log = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(_log))
logging.getLogger().setLevel(logging.DEBUG)


def problems_sent(client):
    out = []
    for request in client.requests:
        message = request["messages"][0]["content"]
        if "Your previous answer had these problems" in message:
            out.append(message.split("Your previous answer had these problems")[1])
    return out


def run(client, pages=None, settings=SETTINGS):
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        result = triage(pages or invented_packet(), settings, client=client)
    SURFACES.extend([captured.getvalue(), *result.warnings, *problems_sent(client)])
    SURFACES.extend(f"{f.code} {f.document} {f.detail}" for f in result.flags)
    SURFACES.extend(d.treatment_reason for d in result.documents if d.model_suggested_treatment)
    return result


def summary(result):
    return [(d.file_id, d.first_sheet, d.last_sheet, d.doc_type,
             d.date.isoformat() if d.date else None, d.treatment) for d in result.documents]


def codes(result, document=None):
    return sorted(f.code for f in result.flags if document is None or f.document == document)


def doc_by_range(result, file_id, first):
    return next(d for d in result.documents if d.file_id == file_id and d.first_sheet == first)


def covered_once(result, sheets_by_file):
    for file_id, n in sheets_by_file.items():
        sheets = sorted(s for d in result.documents if d.file_id == file_id
                        for s in range(d.first_sheet, d.last_sheet + 1))
        if sheets != list(range(1, n + 1)):
            return False
    return True


MAP_SUMMARY = [(d["file"], d["first"], d["last"], d["type"], d["date"], d["treatment"])
               for d in MAP["documents"]]
SHEETS = {name: info["sheets"] for name, info in MAP["files"].items()}
MAP_DOC = {d["id"]: d for d in MAP["documents"]}

# ---------------------------------------------------------------------------
# 1. Types, table and schema
# ---------------------------------------------------------------------------
TABLE = load_treatments(SETTINGS.default_treatments)
for d in MAP["documents"]:
    check(d["type"] in DOC_TYPES, f"map type {d['type']} is in the doc_type enum")
    check(d["type"] in TABLE, f"map type {d['type']} is in default_treatments.json")
check(set(TABLE) == set(DOC_TYPES), "the table gives every doc_type exactly one treatment")

schema = TriageAnswer.model_json_schema()
check(strict_schema_problems(schema) == [], "the record_triage schema meets the strict-mode limits")
tool = tool_definition()
check(tool["strict"] is True and tool["cache_control"] == {"type": "ephemeral"} and tool["name"] == TOOL_NAME,
      "strict tool with cache_control")
doc_schema = schema["$defs"]["TriageDocument"]
check(set(doc_schema["required"]) == set(doc_schema["properties"]), "every TriageDocument field is required")
check("null" not in json.dumps(schema), "no field is nullable; 'unknown' stands in for null")

req = build_request(SETTINGS, "hello")
check(req["tool_choice"] == {"type": "tool", "name": TOOL_NAME}, "forced tool choice on the default model")
check(req["system"][0]["cache_control"] == {"type": "ephemeral"} and req["tools"][0]["cache_control"],
      "system prompt and tool are cached")
check(req["thinking"] == {"type": "adaptive"} and req["output_config"] == {"effort": "medium"}
      and req["max_tokens"] == 16000 and "budget_tokens" not in json.dumps(req),
      "adaptive thinking, effort medium, no manual thinking budget")
req = build_request(replace(SETTINGS, triage_model="claude-opus-5-5"), "hello")
check(req["tool_choice"] == {"type": "auto"}, "models that reject forced tool choice get auto")

check(SETTINGS.triage_model == SETTINGS.model and SETTINGS.triage_window_pages == 250
      and SETTINGS.triage_window_overlap == 10 and SETTINGS.triage_preview_chars == 1500,
      "triage settings default as documented")
os.environ["CANETRUST_TRIAGE_WINDOW_OVERLAP"] = "300"
try:
    load_settings()
    bad_overlap = False
except ValueError:
    bad_overlap = True
finally:
    del os.environ["CANETRUST_TRIAGE_WINDOW_OVERLAP"]
check(bad_overlap, "an overlap not smaller than the window is refused")

# ---------------------------------------------------------------------------
# 2. Previews
# ---------------------------------------------------------------------------
pages = invented_packet()
previews = build_previews(pages, max_chars=SETTINGS.triage_preview_chars)
by_key = {(p.file_id, p.sheet): p for p in previews}
TRUST_FILE = MAP_DOC["trust"]["file"]
PACKET_FILE = MAP_DOC["will"]["file"]
check(len(previews) == sum(SHEETS.values()), "one preview per sheet")
first = by_key[(TRUST_FILE, 1)]
check(first.text.startswith(f"[{TRUST_FILE} sheet 1]\n"), "preview header names file and sheet")
check(first.features.um_mentions == 1 and "um_mentions=1" in first.text, "University mentions are counted")
check(first.features.dates == ("November 28, 2022",), "the date on the title sheet is found")
check(by_key[(TRUST_FILE, 7)].features.printed == "2" and by_key[(TRUST_FILE, 7)].features.doc_numbers == ("48210001",),
      "printed number and footer document number are features")
check(by_key[(PACKET_FILE, 18)].features.forms == ("Form No. X-1",), "form numbers are features")
check(by_key[(PACKET_FILE, 3)].features.printed == "Page 2", "a 'Page N' header is the printed candidate")
check(all(len(p.text) <= SETTINGS.triage_preview_chars for p in previews), "previews respect the character cap")
long_page = PageRecord("f", 1, "\n".join([MARKER + " " + "word " * 80] + BODY * 3))
long_preview = build_previews({"f": [long_page]}, max_chars=1500)[0]
check(all(len(line) <= 161 for line in long_preview.text.splitlines()[1:-1]), "preview lines are cut at 160 characters")
check(len(long_preview.text.splitlines()) == 1 + 2 + 1 + 6 + 1, "first 2 and last 6 lines, with an ellipsis between")
revoking_page = PageRecord("f", 1, "\n".join(
    ["Fourth Amendment", "Heading two"] + BODY[:2]
    + ["1. Revocation of the Earlier Restatement. The earlier restatement and",
       "Restatement dated May 2, 2013 is revoked in its entirety and has no effect."]
    + BODY + ["The Trustor hereby amends Article 4.", "Another revoked line", "A fifth revocation line"]
    + BODY + ["Signature", "Page 1"]))
rp = build_previews({"f": [revoking_page]}, max_chars=1500)[0].text.splitlines()
key_at = rp.index("key lines:")
check(rp[3] == "key lines:" and rp[key_at + 1].startswith("1. Revocation") and "May 2, 2013 is revoked" in rp[key_at + 2],
      "a revocation in the middle of a sheet reaches the preview as key lines, with its date")
check(rp[key_at + 3] == "The Trustor hereby amends Article 4." and rp[key_at + 4] == "Another revoked line"
      and rp[key_at + 5] == "...", "at most 4 key lines, then the ellipsis")
f = page_features("Header\nbody\nAcct ref #12345678v2\n20231005\nForm No. 5.310 1 of 2", max_bare=12)
check(f.doc_numbers == ("#12345678v2", "20231005") and f.forms == ("Form No. 5.310",),
      "'#NNNNNNNNvN' and bare 6+ digit runs are footer document numbers")
check(page_features("Signed on the 3rd day of March, 2020 and 04/05/2021", max_bare=12).dates
      == ("3rd day of March, 2020", "04/05/2021"), "ordinal and numeric dates are found")
check(estimate_tokens(previews) == sum(len(p.text) for p in previews) // 4 + 1 > 1000,
      "token estimate is about 4 characters per token")
check(estimate_tokens(previews[:10]) < estimate_tokens(previews), "the estimate grows with the previews")

# ---------------------------------------------------------------------------
# 3. Round trip
# ---------------------------------------------------------------------------
client = StubClient(truth_answer)
result = run(client)
check(summary(result) == MAP_SUMMARY, "file, first, last, type, date and treatment match the map")
counts = {"documents": len(result.documents),
          **{t: sum(d.treatment == t for d in result.documents) for t in ("annotate", "facts_only", "log_only")}}
check(counts == {k: v for k, v in MAP["expected_counts"].items() if k != "chains"}, "expected_counts match")
mismatches = []
for d in result.documents:
    for page in d.pages:
        want = LABELS.get((d.file_id, page.position))
        if want and want != (page.label, page.label_source):
            mismatches.append((d.file_id, page.position, want, (page.label, page.label_source)))
check(not mismatches, f"every asserted label and source matches through the labeler: {mismatches[:5]}")
check(sum(1 for key in LABELS) == sum(1 for d in result.documents for p in d.pages if (d.file_id, p.position) in LABELS),
      "every asserted label was compared")
check(not result.flags and not result.needs_attention and result.retries_used == 0,
      f"a clean answer gives no flags, no retry: {codes(result)}")
check(not result.warnings, f"no warnings on the clean packet: {result.warnings[:3]}")
check(result.usage["calls"] == 1 and result.usage["input_tokens"] == 1000
      and result.usage["cache_read_input_tokens"] == 800 and result.usage["preview_tokens_estimate"] > 0,
      "usage is reported")
check([d.id for d in result.documents] == [f"d{i}" for i in range(1, 11)], "document ids in file order")
payload = json.dumps(result.to_dict())
check('"label_source": "printed"' in payload and '"text"' not in payload, "to_dict is JSON with labels and no page text")
check(len(client.requests) == 1 and f"[{PACKET_FILE} sheet 21]" in client.requests[0]["messages"][0]["content"],
      "one call carrying every sheet's preview")

# ---------------------------------------------------------------------------
# 4. Validation: each failure, the one-retry success, and repair
# ---------------------------------------------------------------------------
def broken(mutate):
    def answer(request):
        raw = truth_answer(request)
        mutate(raw["documents"])
        return raw
    return answer


def index_of(docs, doc_id):
    d = MAP_DOC[doc_id]
    return next(i for i, x in enumerate(docs) if x["file_id"] == d["file"] and x["first_sheet"] == d["first"])


def drop_cover_letter(docs):
    docs.pop(index_of(docs, "cover_letter"))


def overlap_will(docs):
    docs[index_of(docs, "will")]["last_sheet"] += 1


def outside_trust(docs):
    docs[index_of(docs, "trust")]["last_sheet"] = 49


def reversed_receipt(docs):
    docs[index_of(docs, "receipt_signed")]["first_sheet"] = 3


def bad_date(docs):
    docs[index_of(docs, "codicil")]["date"] = "2022-11-31"


def bad_type(docs):
    docs[index_of(docs, "letters_of_administration")]["doc_type"] = f"{MARKER}_type"


def unknown_file(docs):
    docs[index_of(docs, "notice_to_creditors")]["file_id"] = f"{MARKER}.pdf"


CASES = [
    (drop_cover_letter, f"{PACKET_FILE} sheets 2-3: not in any document (2 sheets)"),
    (overlap_will, f"{PACKET_FILE} sheet 14: in more than one document (1 sheets)"),
    (outside_trust, f"range is outside {TRUST_FILE} sheets 1-48"),
    (reversed_receipt, "first_sheet is after last_sheet"),
    (bad_date, "date is not YYYY-MM-DD or 'unknown'"),
    (bad_type, "invalid or missing field(s) doc_type"),
    (unknown_file, "file_id is not one of the files shown"),
]
for mutate, problem in CASES:
    client = StubClient(broken(mutate), truth_answer)
    result = run(client)
    sent = problems_sent(client)
    check(len(client.requests) == 2 and sent and problem in sent[0],
          f"{mutate.__name__}: the retry lists the problem ({problem})")
    check(summary(result) == MAP_SUMMARY and result.retries_used == 1 and not result.needs_attention,
          f"{mutate.__name__}: the retry's correct answer is used")

client = StubClient(lambda r: None, truth_answer)
result = run(client)
check(f"no {TOOL_NAME} call was made" in problems_sent(client)[0] and summary(result) == MAP_SUMMARY,
      "a response without the tool call is retried")


def many_faults(docs):
    drop_cover_letter(docs)
    overlap_will(docs)
    bad_date(docs)
    bad_type(docs)


client = StubClient(broken(many_faults))
result = run(client)
check(len(client.requests) == 2 and result.retries_used == 1 and result.needs_attention,
      "an answer still invalid after one retry is repaired and needs attention")
check(covered_once(result, SHEETS), "the repaired map covers every sheet exactly once")
gap = doc_by_range(result, PACKET_FILE, 2)
check((gap.last_sheet, gap.doc_type, gap.treatment) == (3, "unknown", "log_only")
      and codes(result, gap.id) == ["type_unknown", "unassigned"], "unclaimed sheets become an unknown document flagged unassigned")
check(doc_by_range(result, PACKET_FILE, 5).last_sheet == 14 and doc_by_range(result, PACKET_FILE, 15).last_sheet == 17,
      "in an overlap the earlier claim keeps the sheet")
codicil = doc_by_range(result, PACKET_FILE, 15)
check(codicil.date is None and "date_unreadable" in codes(result, codicil.id), "an unparseable date becomes None, flagged")
letters = doc_by_range(result, PACKET_FILE, 4)
check(letters.doc_type == "unknown" and "type_unknown" in codes(result, letters.id), "an invalid type becomes unknown")
check(any("repaired" in w for w in result.warnings), "the repair is reported in warnings")

client = StubClient(lambda r: None)
result = run(client)
check(result.needs_attention and covered_once(result, SHEETS) and len(result.documents) == 3
      and all(d.doc_type == "unknown" and "unassigned" in codes(result, d.id) for d in result.documents),
      "no answer at all: each file becomes one unassigned document")

try:
    triage({"f": []}, SETTINGS, client=StubClient(truth_answer))
    raised = False
except TriageError as exc:
    raised = MARKER not in str(exc)
check(raised, "an empty file is refused")

# ---------------------------------------------------------------------------
# 5. The default-treatments table
# ---------------------------------------------------------------------------
result = run(StubClient(lambda r: truth_answer(r, overrides={
    "receipt_blank": {"proposed_treatment": "facts_only"},
    "accounting": {"proposed_treatment": "annotate"},
    "receipt_signed": {"is_blank_form": True, "proposed_treatment": "log_only"}})))
blank = doc_by_range(result, PACKET_FILE, 20)
check(blank.treatment == "log_only" and blank.model_suggested_treatment == "facts_only"
      and "treatment_overridden" in codes(result, blank.id), "a blank form is forced to log_only")
signed = doc_by_range(result, PACKET_FILE, 1)
check(signed.treatment == "log_only" and signed.model_suggested_treatment is None
      and codes(result, signed.id) == [], "is_blank_form beats the type's default even when the model agrees")
accounting = doc_by_range(result, MAP_DOC["accounting"]["file"], 1)
check(accounting.treatment == "log_only" and accounting.model_suggested_treatment == "annotate"
      and accounting.model_treatment_reason == f"{MARKER} reason"
      and accounting.treatment_reason == "default for accounting"
      and "treatment_overridden" in codes(result, accounting.id), "the table beats the model; the suggestion is kept and flagged")
detail = next(f.detail for f in result.flags if f.document == accounting.id)
check(detail == "model proposed annotate, table gives log_only", "the flag names the two treatments only")

with tempfile.TemporaryDirectory() as tmp:
    table = json.loads(SETTINGS.default_treatments.read_text())
    table["log_only"].remove("accounting")
    table["annotate"].append("accounting")
    edited = Path(tmp) / "treatments.json"
    edited.write_text(json.dumps(table))
    result = run(StubClient(truth_answer), settings=replace(SETTINGS, default_treatments=edited))
    accounting = doc_by_range(result, MAP_DOC["accounting"]["file"], 1)
    check(accounting.treatment == "annotate" and "treatment_overridden" in codes(result, accounting.id),
          "editing the table file changes the outcome")
    table["annotate"].remove("accounting")
    edited.write_text(json.dumps(table))
    try:
        load_treatments(edited)
        raised = False
    except TriageError as exc:
        raised = "accounting" in str(exc)
    check(raised, "a table missing a type is refused")

# ---------------------------------------------------------------------------
# 6. Cross-check flags
# ---------------------------------------------------------------------------
result = run(StubClient(lambda r: truth_answer(r, overrides={
    "accounting": {"appears_to_gift_to_university": "unclear"},
    "notice_to_creditors": {"appears_to_gift_to_university": "yes"},
    "letters_of_administration": {"doc_type": "unknown", "proposed_treatment": "log_only"},
    "cover_letter": {"confidence": "low"}})))
check("possible_gift_in_non_annotated_document" in codes(result, doc_by_range(result, MAP_DOC["accounting"]["file"], 1).id)
      and "possible_gift_in_non_annotated_document" in codes(result, doc_by_range(result, PACKET_FILE, 21).id),
      "a possible gift in a non-annotated document is flagged (yes and unclear)")
check("possible_gift_in_non_annotated_document" not in codes(result, doc_by_range(result, TRUST_FILE, 1).id),
      "a gift in an annotated document is not flagged")
check(codes(result, doc_by_range(result, PACKET_FILE, 4).id) == ["type_unknown"], "type_unknown")
check(codes(result, doc_by_range(result, PACKET_FILE, 2).id) == ["low_confidence"], "low_confidence")
check("possible_multiple_matters" not in codes(result), "one principal: no multiple-matters flag")

result = run(StubClient(lambda r: truth_answer(r, overrides={"codicil": {"principal": f"Other {MARKER} Person"}})))
flag = [f for f in result.flags if f.code == "possible_multiple_matters"]
check(flag and flag[0].document is None and flag[0].detail == "2 distinct principals", "possible_multiple_matters")
check(normalize_principal(f"{MARKER.upper()}, AMBROSE Q., Jr.") == normalize_principal(PRINCIPAL)
      and normalize_principal("unknown") is None, "principals are normalized for case, punctuation, order and Jr./Sr.")

result = run(StubClient(truth_answer), pages=invented_packet({"codicil": DOC_NUMBERS["will"]}))
codicil = doc_by_range(result, PACKET_FILE, 14)
check(codes(result, codicil.id) == ["footer_id_disagrees_with_boundary"]
      and "sheets 13 and 14" in next(f.detail for f in result.flags if f.document == codicil.id),
      "a footer document number running across a boundary is flagged")
odd = invented_packet()
odd[TRUST_FILE][19] = replace(odd[TRUST_FILE][19], text=odd[TRUST_FILE][19].text.replace("48210001", "48217777"))
result = run(StubClient(truth_answer), pages=odd)
check(codes(result, "d1") == ["footer_id_disagrees_with_boundary"], "a footer number changing inside a document is flagged")
check(summary(result) == MAP_SUMMARY, "cross-checks only add flags; the map is unchanged")

# ---------------------------------------------------------------------------
# 7. Windowing
# ---------------------------------------------------------------------------
SMALL = replace(SETTINGS, triage_window_pages=8, triage_window_overlap=2)
TRUTH = [
    {"id": "w", "file": "a.pdf", "first": 1, "last": 5, "type": "will", "date": "2020-01-02", "treatment": "annotate"},
    {"id": "acc", "file": "a.pdf", "first": 6, "last": 16, "type": "accounting", "date": None, "treatment": "log_only"},
    {"id": "l", "file": "a.pdf", "first": 17, "last": 20, "type": "letter", "date": "2021-03-04", "treatment": "facts_only"},
    {"id": "n", "file": "b.pdf", "first": 1, "last": 1, "type": "notice_to_creditors", "date": None, "treatment": "facts_only"},
    {"id": "r1", "file": "b.pdf", "first": 2, "last": 3, "type": "receipt_of_notice", "date": None, "treatment": "facts_only"},
]


def plain_pages(sizes):
    return {f: [PageRecord(f, s, "\n".join([f"{MARKER} sheet {s}", *BODY])) for s in range(1, n + 1)]
            for f, n in sizes.items()}


client = StubClient(lambda r: truth_answer(r, truth=TRUTH))
result = run(client, pages=plain_pages({"a.pdf": 20, "b.pdf": 3}), settings=SMALL)
check(len(client.requests) == 4 and all(sum(len(v) for v in shown_sheets(q).values()) <= 8 for q in client.requests),
      "23 sheets in windows of 8 overlapping by 2: four calls, none over the window")
check(summary(result) == [(d["file"], d["first"], d["last"], d["type"], d["date"], d["treatment"]) for d in TRUTH],
      "documents cut by window edges are stitched back")
check(any("overlapping windows" in w for w in result.warnings) and result.usage["calls"] == 4, "windowing is reported")

SAME_TYPE = [
    {"id": "x", "file": "c.pdf", "first": 1, "last": 7, "type": "receipt_of_notice", "date": None, "treatment": "facts_only"},
    {"id": "y", "file": "c.pdf", "first": 8, "last": 10, "type": "receipt_of_notice", "date": None, "treatment": "facts_only"},
]
result = run(StubClient(lambda r: truth_answer(r, truth=SAME_TYPE)), pages=plain_pages({"c.pdf": 10}), settings=SMALL)
check([(d.first_sheet, d.last_sheet) for d in result.documents] == [(1, 7), (8, 10)],
      "two same-type documents meeting at a window midpoint stay separate")

client = StubClient(lambda r: truth_answer(r, truth=TRUTH) if "a.pdf" not in shown_sheets(r) or 9 not in shown_sheets(r)["a.pdf"]
                    else {"documents": []})
result = run(client, pages=plain_pages({"a.pdf": 20, "b.pdf": 3}), settings=SMALL)
check(result.needs_attention and covered_once(result, {"a.pdf": 20, "b.pdf": 3}),
      "a window that fails twice is repaired; the stitched map still covers every sheet once")

# ---------------------------------------------------------------------------
# 8. The map comparison used by tools/live_triage.py and test_gay_fixtures.py
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, str(ROOT / "tools"))
from live_triage import compare_to_map

check(compare_to_map(run(StubClient(truth_answer)), MAP) == [], "the round trip has no mismatches against the map")
rows = compare_to_map(run(StubClient(broken(overlap_will), truth_answer)), MAP)
check(rows == [], "a retried answer that ends correct has no mismatches")
rows = compare_to_map(run(StubClient(lambda r: truth_answer(r, overrides={
    "accounting": {"doc_type": "amendment", "proposed_treatment": "annotate"}}))), MAP)
check([r[:3] for r in rows] == [("field", "accounting", "type"), ("field", "accounting", "treatment"),
                                ("count", "-", "annotate"), ("count", "-", "log_only")],
      "a wrong type is reported with its treatment and count effects")


def overlap_and_bad_type(docs):
    overlap_will(docs)
    bad_type(docs)


rows = compare_to_map(run(StubClient(broken(overlap_and_bad_type))), MAP)   # repaired twice-failed answer
kinds = [r[0] for r in rows]
check(kinds[:2] == ["boundary", "boundary"] and "field" in kinds and kinds == sorted(kinds, key=["boundary", "field", "count"].index)
      and {r[1] for r in rows if r[0] == "boundary"} >= {"will", "codicil"},
      "boundary mismatches come first, then fields")
SURFACES.extend(" ".join(r) for r in rows)

# ---------------------------------------------------------------------------
# 8b. revokes[]
# ---------------------------------------------------------------------------
check("revokes" in doc_schema["required"] and doc_schema["properties"]["revokes"]["type"] == "array",
      "revokes is a required list in the strict schema")
check("`revokes`" in SETTINGS.triage_rules.read_text(encoding="utf-8"), "the triage rules explain revokes")
revoking = {"codicil": {"revokes": [{"what": f"{MARKER} earlier codicil", "date": "2022-01-15"},
                                    {"what": f"{MARKER} undated codicil", "date": "unknown"}]}}
result = run(StubClient(lambda r: truth_answer(r, overrides=revoking)))
codicil = doc_by_range(result, PACKET_FILE, MAP_DOC["codicil"]["first"])
check([r["date"] for r in codicil.revokes] == ["2022-01-15", None], "revokes come through with parsed dates")
check(all(d.revokes == [] for d in result.documents if d.id != codicil.id), "other documents revoke nothing")
check(codicil.to_dict()["revokes"][0]["date"] == "2022-01-15", "revokes are in to_dict")
bad_revokes = {"codicil": {"revokes": [{"what": "x", "date": "2022-02-30"}]}}
client = StubClient(lambda r: truth_answer(r, overrides=bad_revokes), truth_answer)
result = run(client)
check("a revokes date is not YYYY-MM-DD or 'unknown'" in problems_sent(client)[0] and result.retries_used == 1,
      "an unparseable revokes date is a validation problem")

# ---------------------------------------------------------------------------
# 9. No document text anywhere it must not be
# ---------------------------------------------------------------------------
SURFACES.append(_log.getvalue())
check(len(SURFACES) > 50, "warnings, flags, problems and logs were collected")
probe = StubClient(truth_answer)
probe_result = run(probe)
check(MARKER in probe.requests[0]["messages"][0]["content"]
      and MARKER in json.dumps(probe_result.to_dict()), "the marker is really in the pages and in the model output")
check(not any(MARKER.lower() in s.lower() for s in SURFACES),
      "the marker in pages and model output never reaches a warning, flag, problem, error or log")

print(f"\nALL {passed} CHECKS PASSED")
