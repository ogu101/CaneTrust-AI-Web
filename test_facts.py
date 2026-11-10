"""
API-free tests for the facts lane (docs/DESIGN.md, "Facts lane"; milestone 5).

    .venv/bin/python test_facts.py

Documents follow fixtures/gay/expected_map.json (ids, types, dates, treatments, sheet counts)
with INVENTED page text. The stub model answers with the facts that fixtures/gay/
expected_facts.json says each document states; the merge must then reproduce that file,
including the planted address conflict (the newest source, the 2025 accounting, wins).
"""

import contextlib
import io
import json
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import testkit  # noqa: F401  (blocks real model calls; disables .env)
from testkit import strict_schema_problems

from facts import (
    FACT_FIELDS, TOOL_NAME, Fact, FactsAnswer, build_message, build_request, contact_rows,
    extract_facts, merge_facts, tool_definition,
)
from page_reader import PageRecord
from settings import load_settings

ROOT = Path(__file__).parent
MAP = json.loads((ROOT / "fixtures" / "gay" / "expected_map.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((ROOT / "fixtures" / "gay" / "expected_facts.json").read_text(encoding="utf-8"))
SETTINGS = load_settings()
MARKER = "Zephyrquill"
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


# ---------------------------------------------------------------------------
# Invented documents with the map's structure
# ---------------------------------------------------------------------------
@dataclass
class Doc:
    id: str
    doc_type: str
    date: date | None
    treatment: str
    pages: list = field(default_factory=list)
    excluded: bool = False


FILLER = f"The {MARKER} specimen sheet carries invented filler words only."
CONTACT_SHEETS = {"trust": {16}, "accounting": {1}}     # Article 11 sheet; the accounting's cover


def invented(d):
    n = d["last"] - d["first"] + 1
    pages = []
    for k in range(n):
        lines = [FILLER, "More invented words for the specimen."]
        if k + 1 in CONTACT_SHEETS.get(d["id"], set()):
            lines.append(f"Successor Trustee {MARKER} Example, 1 Specimen Way, Example, ZZ 00000")
        pages.append(PageRecord(d["file"], d["first"] + k, "\n".join(lines),
                                label=str(k + 1), label_source="inferred"))
    return Doc(d["id"], d["type"], date.fromisoformat(d["date"]) if d["date"] else None,
               d["treatment"], pages)


DOCS = [invented(d) for d in MAP["documents"]]
DOC = {d.id: d for d in DOCS}


def stated_facts():
    """What each document states, according to expected_facts.json."""
    e = EXPECTED
    pr = e["personal_representative"]["name"]
    items = []

    def add(field_name, value, doc_id, subject="unknown", page="1"):
        items.append({"field": field_name, "subject": subject, "value": value,
                      "document_id": doc_id, "page": page})

    for src in e["date_of_death"]["sources"]:
        add("date_of_death", e["date_of_death"]["value"], src)
    for src in e["probate_case"]["sources"]:
        add("probate_case_number", e["probate_case"]["number"], src)
    add("probate_court", e["probate_case"]["court"], "letters_of_administration")
    for src in e["personal_representative"]["sources"]:
        add("personal_representative", pr, src)
    for src in e["successor_trustee"]["sources"]:
        doc_id = src.split()[0]
        add("successor_trustee", e["successor_trustee"]["name"], doc_id, page="11" if doc_id == "trust" else "1")
    add("backup_successor_trustee", e["successor_trustee"]["backup"], "trust", page="11")
    for a in e["addresses_for_mahalie_ann_fulton"]:
        add("address", a["address"], a["source"], subject=pr)
    att = e["estate_attorney"]
    for src in att["sources"]:
        add("attorney", att["name"], src)
        add("attorney_firm", att["firm"], src)
        add("phone", att["phone"], src, subject=att["name"])
        add("email", att["email"], src, subject=att["name"])
    return items


class StubClient:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.messages = self

    def stream(self, **request):
        self.requests.append(request)
        raw = self.answer(request) if callable(self.answer) else self.answer
        content = [] if raw is None else [SimpleNamespace(type="tool_use", name=TOOL_NAME, input=raw)]
        message = SimpleNamespace(content=content, stop_reason="tool_use",
                                  usage=SimpleNamespace(input_tokens=500, output_tokens=100))

        @contextlib.contextmanager
        def ctx():
            yield SimpleNamespace(get_final_message=lambda: message)
        return ctx()


SURFACES = []


def run(answer, documents=DOCS, settings=SETTINGS):
    client = StubClient(answer)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = extract_facts(documents, settings, client=client)
    SURFACES.extend([out.getvalue(), *result.warnings])
    return result, client


# ---------------------------------------------------------------------------
# 1. Schema and request
# ---------------------------------------------------------------------------
check(strict_schema_problems(FactsAnswer.model_json_schema()) == [], "record_facts meets the strict-mode limits")
tool = tool_definition()
check(tool["strict"] is True and tool["name"] == TOOL_NAME, "strict record_facts tool")
item_schema = FactsAnswer.model_json_schema()["$defs"]["FactItem"]
check(set(item_schema["required"]) == set(item_schema["properties"]), "every FactItem field is required")
check(not {"bequest", "income", "restriction"} & {w for f in FACT_FIELDS for w in f.split("_")},
      "no fact field touches the bequest, income or restriction fields")
req = build_request(SETTINGS, "hello")
check(req["tool_choice"] == {"type": "tool", "name": TOOL_NAME} and req["model"] == SETTINGS.triage_model
      and req["max_tokens"] == SETTINGS.facts_max_tokens == 8000, "forced call on the triage model, 8000 tokens")
check(build_request(replace(SETTINGS, triage_model="claude-opus-5-5"), "x")["tool_choice"] == {"type": "auto"},
      "models that reject forced tool choice get auto")

# ---------------------------------------------------------------------------
# 2. What the model is shown
# ---------------------------------------------------------------------------
message, shown, warnings = build_message(DOCS, max_chars=SETTINGS.facts_max_chars)
facts_only = [d.id for d in DOCS if d.treatment == "facts_only"]
check(set(facts_only) <= set(shown), "every facts_only document is shown")
check("trust" in shown and "accounting" in shown, "contact pages of the trust and the (log only) accounting are shown")
check("will" not in shown and "codicil" not in shown and "receipt_blank" not in shown,
      "documents without contact pages are not shown")
trust_block = message.split("=== Document trust")[1].split("=== Document")[0]
check(trust_block.count("[Page ") == 1 and "[Page 16]" in trust_block and "contact pages only" in trust_block,
      "only the trust's contact page is sent, with its printed label")
letter_block = message.split("=== Document cover_letter")[1].split("=== Document")[0]
check(letter_block.count("[Page ") == 2 and "full text" in letter_block, "facts_only documents are sent in full")
excluded = [replace(d, excluded=True) if d.id == "cover_letter" else d for d in DOCS]
check("cover_letter" not in build_message(excluded, max_chars=10**6)[1], "excluded documents are never sent")
_, small_shown, small_warn = build_message(DOCS, max_chars=200)
check(len(small_shown) < len(shown) and small_warn and all(MARKER not in w for w in small_warn),
      "the character cap leaves documents out, with a warning")

# ---------------------------------------------------------------------------
# 3. expected_facts.json, reproduced
# ---------------------------------------------------------------------------
result, client = run({"facts": stated_facts()})
check(len(client.requests) == 1 and result.usage["calls"] == 1, "one call")
check(not result.warnings, f"no warnings: {result.warnings}")
E = EXPECTED


def one(field_name, subject=None):
    found = result.get(field_name, subject)
    assert len(found) == 1, (field_name, len(found))
    return found[0]


dod = one("date_of_death")
check(dod.value == E["date_of_death"]["value"] and not dod.conflict
      and {f.source_document for f in dod.sources} == set(E["date_of_death"]["sources"]),
      "date of death with both sources")
case = one("probate_case_number")
check(case.value == E["probate_case"]["number"]
      and {f.source_document for f in case.sources} == set(E["probate_case"]["sources"]), "probate case number")
check(one("probate_court").value == E["probate_case"]["court"], "probate court")
pr = one("personal_representative")
check(pr.value == E["personal_representative"]["name"]
      and {f.source_document for f in pr.sources} == set(E["personal_representative"]["sources"]),
      "personal representative with its three sources")
st = one("successor_trustee")
check(st.value == E["successor_trustee"]["name"] and one("backup_successor_trustee").value == E["successor_trustee"]["backup"]
      and {f.source_document for f in st.sources} == {s.split()[0] for s in E["successor_trustee"]["sources"]},
      "successor trustee and backup")
addr = one("address", E["personal_representative"]["name"])
want = {a["source"]: a["address"] for a in E["addresses_for_mahalie_ann_fulton"]}
check(addr.conflict and len(addr.values) == 2 and set(addr.values) == set(want.values()),
      "the address conflict is kept: both addresses are shown")
check(addr.value == want["accounting"] and addr.sources[0].source_date == date(2025, 6, 27),
      "the newest source (the 2025 accounting) wins")
check(addr.sources[-1].source_document == "notice_of_administration" and addr.sources[-1].source_date is None,
      "the undated notice counts as oldest")
att = E["estate_attorney"]
check(one("attorney").value == att["name"] and one("attorney_firm").value == att["firm"]
      and one("phone", att["name"]).value == att["phone"] and one("email", att["name"]).value == att["email"]
      and {f.source_document for f in one("attorney").sources} == set(att["sources"]), "estate attorney and contacts")
check((not result.get("phone", E["personal_representative"]["name"])
       and not result.get("email", E["personal_representative"]["name"]))
      == E["no_personal_phone_or_email_for_representative"],
      "no personal phone or e-mail for the representative")
check(st.sources[0].source_document == "accounting" and st.sources[0].source_date == date(2025, 6, 27)
      and next(f for f in st.sources if f.source_document == "trust").page == "11",
      "sources carry the triage date and the cited page label")

rows = contact_rows(result)
by_label = {r["label"]: r for r in rows}
pr_row = by_label["Personal representative"]
check(want["accounting"] in pr_row["answer"] and want["notice_of_administration"] not in pr_row["answer"],
      "the contact row shows the preferred address")
check("2025-06-27" in pr_row["citation"] and "accounting" in pr_row["citation"]
      and "notice_of_administration (undated" in pr_row["citation"], "the citation column names sources and dates")
check(pr_row["note"] and want["notice_of_administration"] in pr_row["note"] and pr_row["status"] == "Ambiguous",
      "the conflict is in the note column, with both values")
check(by_label["Attorney"]["status"] == "Found" and att["phone"] in by_label["Attorney"]["answer"],
      "attorney row with contacts")
check(set(by_label) == {"Personal representative", "Trustee / successor trustees", "Date of death",
                        "Probate case", "Attorney"}, "contact rows only, no dispositive rows")
check(json.dumps(result.to_dict()) and result.to_dict()["facts"][0]["sources"], "to_dict is JSON")

# ---------------------------------------------------------------------------
# 4. Bad answers
# ---------------------------------------------------------------------------
bad = [
    {"field": "date_of_death", "subject": "unknown", "value": f"{MARKER} 1", "document_id": f"{MARKER}", "page": "1"},
    {"field": f"{MARKER}_field", "subject": "unknown", "value": "x", "document_id": "cover_letter", "page": "1"},
    {"field": "attorney", "subject": "unknown", "value": f"{MARKER} Esq.", "document_id": "cover_letter", "page": f"{MARKER}"},
    {"field": "phone", "subject": "unknown", "value": "unknown", "document_id": "cover_letter", "page": "1"},
]
result, _ = run({"facts": bad})
check([f.field for f in result.facts] == ["attorney"] and result.facts[0].sources[0].page is None,
      "invalid items and unknown documents are dropped; a bad page label is cleared")
check(len(result.warnings) == 3, f"each problem is a warning: {result.warnings}")
result, _ = run(None)
check(result.facts == [] and "no record_facts call was made" in result.warnings[0], "no tool call gives no facts")
result, client = run({"facts": []}, documents=[replace(DOC["will"])])
check(result.facts == [] and not client.requests, "nothing to show: no call at all")

# ---------------------------------------------------------------------------
# 5. Merge rules
# ---------------------------------------------------------------------------
d1, d2 = date(2020, 1, 1), date(2021, 1, 1)
merged = merge_facts([Fact("phone", "(941) 364-2723", "Ann Lee", "a", d1),
                      Fact("phone", "941.364.2723", "LEE, ANN", "b", d2)])
check(len(merged) == 1 and not merged[0].conflict and merged[0].value == "941.364.2723",
      "phone numbers compare by digits; subjects by normalized name; newest first")
merged = merge_facts([Fact("trustee", "Ann Lee", None, "a", d1), Fact("trustee", "Bo Chu", None, "b", d1)],
                     order={"a": 0, "b": 1})
check(merged[0].conflict and merged[0].value == "Bo Chu", "same-date conflict: the later document wins")
merged = merge_facts([Fact("trustee", "Ann Lee", None, "a", None), Fact("trustee", "Bo Chu", None, "b", d1)])
check(merged[0].value == "Bo Chu", "a dated source beats an undated one")

# ---------------------------------------------------------------------------
# 6. No document text in warnings or output
# ---------------------------------------------------------------------------
check(len(SURFACES) > 3 and not any(MARKER in s for s in SURFACES), "the marker never reaches a warning or log")

print(f"\nALL {passed} CHECKS PASSED")
