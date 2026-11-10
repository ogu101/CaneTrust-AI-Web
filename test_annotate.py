"""
API-free tests for annotation (annotate.py, annotation_schema.py; milestone 6).

    .venv/bin/python test_annotate.py

Ports the schema, reconciliation and request checks of the retired test_multi.py to the one
annotation path, and adds the n = 0 case, chain inputs built from chains.py, printed page
markers, the cited-page check and parallel annotation where one chain fails. FICTIONAL data.
"""

import json
import threading
import time
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx

from testkit import StubModel, chain_annotation, strict_schema_problems

import annotate
from annotate import (
    AnnotationError, ChainInput, Instrument, annotate_chain, annotate_chains, build_request,
    chain_input, check_citations, render_chain, tool_definition,
)
from annotation_schema import AmendmentEntry, ChainAnnotation, Finding, InstrumentInfo, reconcile
from chains import ChainDoc, build_chains
from page_reader import PageRecord
from settings import load_settings

SETTINGS = load_settings()
MARKER = "Zephyrquill"
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


# ---------------------------------------------------------------------------
# 1. Schema and reconciliation
# ---------------------------------------------------------------------------
tool = tool_definition()
check("strict" not in tool and annotate.STRICT is False,
      "the annotation tool is sent without strict (the API rejects its grammar; PROGRESS D0.1)")
check(tool["name"] == "analyze_documents" and tool["cache_control"] == {"type": "ephemeral"}, "cached tool")
schema = ChainAnnotation.model_json_schema()
check("FieldHistory" in schema["$defs"] and "operative" not in schema["$defs"]["FieldHistory"]["required"],
      "operative may be omitted (null when there are no amendments)")
check(any("additionalProperties is not false" in p for p in strict_schema_problems(schema)) is False,
      "every object is still closed (additionalProperties false)")

ann = chain_annotation()
h = ann.bequest_language.um_receives
h.amendments = [h.amendments[1], h.amendments[0], AmendmentEntry(
    amendment_number=9, effect="Changed", description="bogus", citation="")]
warnings = reconcile(ann, [1, 2, 3])
check([e.amendment_number for e in h.amendments] == [1, 2, 3], "entries put back in chain order")
check(h.amendments[2].effect == "Could not determine", "a skipped amendment entry becomes a visible 'Could not determine'")
check(any("unexpected" in w for w in warnings) and any("no entry for amendment 3" in w for w in warnings),
      "reconcile reports both the missing and the unexpected entry")
check(AmendmentEntry(amendment_number=1, effect="no CHANGE", description="", citation="").effect == "No change",
      "effect casing is normalized")
check(Finding(status="not in this Document").status == "Not in this document", "status casing is normalized")
check(InstrumentInfo(amendment_number=1, form="Changes_Only", revokes=[], note="").form == "changes_only",
      "form casing is normalized")

ann = chain_annotation((1, 2))
ann.instruments = ann.instruments[:1]
ann.restrictions.other_restrictions.operative = None
warnings = reconcile(ann, [1, 2])
check([i.amendment_number for i in ann.instruments] == [1, 2] and ann.instruments[1].form == "unclear",
      "a missing instrument classification is filled as 'unclear'")
check(ann.restrictions.other_restrictions.operative.status == "Could not determine"
      and any("no operative position" in w for w in warnings), "a missing operative position is visible")

ann0 = chain_annotation(())
check(all(x.operative is None for x in (ann0.bequest_language.location, ann0.um_mentions[0])), "n = 0 canned answer has null operative")
check(reconcile(ann0, []) == [] and ann0.bequest_language.location.operative.answer == "original location",
      "n = 0: operative becomes the original, without warnings")
raw = chain_annotation((1,)).model_dump(mode="json")
raw["bequest_language"]["location"].pop("operative")
check(ChainAnnotation.model_validate(raw).bequest_language.location.operative is None, "an omitted operative validates")

# ---------------------------------------------------------------------------
# 2. Chain inputs from chains.py, and what the model reads
# ---------------------------------------------------------------------------
def pages(n, start_label=1, empty=()):
    return [PageRecord("f", i + 1, "" if i + 1 in empty else f"{MARKER} invented text " * 5,
                       label=str(start_label + i), label_source="printed") for i in range(n)]


DOCS = {
    "t": SimpleNamespace(title="Fictional Trust", doc_type="trust", date=date(2000, 1, 1), pages=pages(3)),
    "a1": SimpleNamespace(title="First Amendment", doc_type="amendment", date=date(2005, 1, 1), pages=pages(1)),
    "r": SimpleNamespace(title="Restatement", doc_type="restated_trust", date=date(2010, 1, 1), pages=pages(4, empty={2})),
    "a2": SimpleNamespace(title="Second Amendment", doc_type="amendment", date=date(2012, 1, 1), pages=pages(1)),
    "a3": SimpleNamespace(title="Third Amendment", doc_type="amendment", date=date(2014, 1, 1), pages=pages(2)),
}
cdocs = [ChainDoc("t", "trust", "P", date(2000, 1, 1), "annotate", 1),
         ChainDoc("a1", "amendment", "P", date(2005, 1, 1), "annotate", 2),
         ChainDoc("r", "restated_trust", "P", date(2010, 1, 1), "annotate", 3,
                  refers_to=({"what": "amendment", "date": "2007-07-07"},)),
         ChainDoc("a3", "amendment", "P", date(2014, 1, 1), "annotate", 5,
                  refers_to=({"what": "second amendment", "date": "2013-03-03"},),
                  revokes=({"what": "x", "date": "2012-01-01"},)),
         ChainDoc("a2", "amendment", "P", date(2012, 1, 1), "annotate", 4)]
chain = build_chains(cdocs).chains[0]
ci = chain_input(chain, DOCS, "Fictional chain")
check([(i.tag, i.doc_id, i.status) for i in ci.instruments] == [
    ("Trust", "r", "BASE"), ("Amendment 1", "a2", "REVOKED by Amendment 2"), ("Amendment 2", "a3", "ACTIVE")],
    "instruments: base first, amendments by date, a revoked one marked with its revoker's tag")
check(ci.amendment_numbers == [1, 2] and ci.has_base, "amendment numbers 1..n")
text = render_chain(ci)
check(text.index("=== Trust [BASE]") < text.index("=== Amendment 1") < text.index("=== Amendment 2"),
      "the chain text is in reading order")
check("Document family: trust" in text and "Amendment numbers to report on, exactly: [1, 2]" in text,
      "family and amendment numbers are stated")
check("Superseded by the base" in text and "Fictional Trust, 2000-01-01 (t)" in text and "amendment, 2007-07-07" in text,
      "superseded instruments (provided or only referenced) are named, not as gaps")
check("NOT PROVIDED" in text and "second amendment, 2013-03-03" in text, "a gap in the chain is stated to the model")
check("[Page 1]" in text and "[Page 2: no extractable text]" in text and "[PDF page" not in text,
      "printed page markers from the labeler; an unreadable page is marked; no PDF positions")

will = build_chains([ChainDoc("w", "will", "P", date(2001, 1, 1), "annotate", 1),
                     ChainDoc("c", "codicil", "P", date(2002, 1, 1), "annotate", 2)]).chains[0]
wci = chain_input(will, {"w": SimpleNamespace(title="Will", doc_type="will", date=date(2001, 1, 1), pages=pages(2)),
                         "c": SimpleNamespace(title="Codicil", doc_type="codicil", date=date(2002, 1, 1), pages=pages(1))},
                  "Will chain")
check([i.tag for i in wci.instruments] == ["Will", "Codicil 1"] and "Document family: will" in render_chain(wci),
      "a will chain: the will is the base and the codicil is Codicil 1")
orphan = build_chains([ChainDoc("c", "codicil", "P", date(2002, 1, 1), "annotate", 1)]).chains[0]
oci = chain_input(orphan, {"c": SimpleNamespace(title="Codicil", doc_type="codicil", date=date(2002, 1, 1), pages=pages(1))}, "x")
check(not oci.has_base and oci.amendment_numbers == [1] and "BASE INSTRUMENT: NOT PROVIDED" in render_chain(oci),
      "a chain with no base says so")
single = ChainInput("s1", "trust", "Alone", [Instrument("Trust", 0, "t", "T", "trust", date(2000, 1, 1), "BASE", False, pages(2))], True)
check("This chain has no amendments" in render_chain(single) and "exactly:" not in render_chain(single),
      "n = 0 is stated plainly")

req = build_request(ci, SETTINGS)
check(req["tool_choice"] == {"type": "tool", "name": "analyze_documents"} and req["thinking"] == {"type": "adaptive"},
      "forced tool choice and adaptive thinking on the default model")
check(req["max_tokens"] == SETTINGS.max_tokens_annotate == 48000 and req["model"] == SETTINGS.model,
      "one token budget for every chain")
check(req["system"][0]["cache_control"] and "Wills, pour-overs" in req["system"][0]["text"]
      and "not a gift to the University" in req["system"][0]["text"],
      "one rules file, cached, with the will rule")
check("[PDF page" not in req["system"][0]["text"] and "p. <label>" in req["system"][0]["text"],
      "the rules cite printed labels, never PDF pages")
from dataclasses import replace
req = build_request(ci, replace(SETTINGS, model="claude-opus-5-5"))
check(req["tool_choice"] == {"type": "auto"} and "Call analyze_documents exactly once" in req["messages"][0]["content"],
      "models that reject forced tool choice get auto plus an instruction")

# ---------------------------------------------------------------------------
# 3. Cited page labels
# ---------------------------------------------------------------------------
ann = chain_annotation((1, 2))
reconcile(ann, [1, 2])
check(check_citations(ann, ci) == [], "the canned citations (page 1 of each instrument) all exist")
ann.bequest_language.location.original.citation = "Trust, Art. 5, p. 4; Trust, p. 9"
ann.bequest_language.um_receives.amendments[1].citation = "Amendment 2, §3, pp. 1-3"
ann.bequest_language.um_receives.operative.citation = "Amendment 2, p. 2; Trust, Art. II, page 7"
ann.restrictions.other_restrictions.original.citation = "Art. 3, p. iv"
ann.found_but_uncategorized[0].citation = "p. 99"
problems = check_citations(ann, ci)
check(sorted(problems) == sorted([
    ("bequest_language.location.original", "Trust", "9"),
    ("bequest_language.um_receives.amendments[2]", "Amendment 2", "3"),
    ("bequest_language.um_receives.operative", "Trust", "7"),
    ("restrictions.other_restrictions.original", "Trust", "iv"),
    ("found_but_uncategorized[0]", "this chain", "99")]), f"each unknown label is found: {problems}")
check("[Check: p. 9 is not a page of Trust.]" in ann.bequest_language.location.original.note,
      "the problem is noted in that cell for the reviewer")
check("[Check: p. 3 is not a page of Amendment 2.]" in ann.bequest_language.um_receives.amendments[1].description,
      "amendment cells get the note in their text")
uci = ChainInput("u", "will", "u", [Instrument("Will", 0, "w", "W", "will", None, "BASE", False,
                                               [PageRecord("f", 1, "x", label="unnumbered, sheet 1 of 2"),
                                                PageRecord("f", 2, "x", label="unnumbered, sheet 2 of 2")])], True)
ann0 = chain_annotation((), base="Will")
reconcile(ann0, [])
ann0.bequest_language.location.original.citation = "Will, Art. 3, p. unnumbered, sheet 2 of 2"
ann0.bequest_language.trigger_event.original.citation = "Will, p. unnumbered, sheet 3 of 2"
for h in (ann0.bequest_language.location, ann0.bequest_language.trigger_event):
    h.operative.citation = h.original.citation
unnumbered = [p for p in check_citations(ann0, uci) if p[2].startswith("unnumbered")]
check([p[2] for p in unnumbered] == ["unnumbered, sheet 3 of 2"] * 2
      and all(p[0].startswith("bequest_language.trigger_event") for p in unnumbered),
      "'unnumbered, sheet N of M' labels are checked too")

# ---------------------------------------------------------------------------
# 4. One call, and parallel chains where one fails
# ---------------------------------------------------------------------------
stub = StubModel()
outcome = annotate_chain(ci, SETTINGS, stub)
check(outcome.annotation is not None and outcome.annotation.document_name == "Fictional chain"
      and outcome.usage["calls"] == 1 and outcome.citation_problems == [], "a chain is annotated and validated")
check(len(stub.requests) == 1 and stub.requests[0]["tools"][0]["name"] == "analyze_documents", "one call per chain")

for answer, fragment in ((None, "no analyze_documents call"),):
    try:
        annotate_chain(ci, SETTINGS, StubModel({"analyze_documents": answer}))
        check(False, "no tool call must fail")
    except AnnotationError as exc:
        check(fragment in str(exc) and MARKER not in str(exc), "no tool call is a clear error without document text")
bad = chain_annotation((1, 2)).model_dump(mode="json")
bad["bequest_language"]["location"]["original"]["status"] = f"{MARKER} status"
try:
    annotate_chain(ci, SETTINGS, StubModel({"analyze_documents": bad}))
    check(False, "an invalid answer must fail")
except Exception as exc:
    check(type(exc).__name__ == "ValidationError", "an invalid answer fails validation")


class MaxTokens(StubModel):
    def stream(self, **request):
        ctx = super().stream(**request)
        msg = ctx.__enter__().get_final_message()
        msg.stop_reason = "max_tokens"
        import contextlib

        @contextlib.contextmanager
        def c():
            yield SimpleNamespace(get_final_message=lambda: msg)
        return c()


try:
    annotate_chain(ci, SETTINGS, MaxTokens())
    check(False, "a cut-off answer must fail")
except AnnotationError as exc:
    check("cut off at max_tokens" in str(exc), "a cut-off answer is an error, never a partial workbook")
big = ChainInput("b", "trust", "b", [Instrument(f"Amendment {n}", n, f"x{n}", "x", "amendment", None, "ACTIVE", False, pages(1))
                                     for n in range(1, 10)], False)
try:
    annotate_chain(big, SETTINGS, StubModel())
    check(False, "nine amendments must be refused")
except AnnotationError as exc:
    check("9 amendments" in str(exc) and "room for 8" in str(exc), "more amendments than columns is refused clearly")

request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class Selective(StubModel):
    """Fails the chain named 'Will chain'; the rest succeed, slowly, to show they run together."""
    active = 0
    peak = 0

    def stream(self, **req):
        with self.lock:
            Selective.active += 1
            Selective.peak = max(Selective.peak, Selective.active)
        time.sleep(0.2)
        with self.lock:
            Selective.active -= 1
        if "Chain: Will chain" in req["messages"][0]["content"]:
            raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
        return super().stream(**req)


messages = []
lock = threading.Lock()
stub = Selective()
outcomes = annotate_chains([ci, wci, single], SETTINGS, lambda m: messages.append(m), make_client=lambda: stub)
check([o.chain_id for o in outcomes] == ["c1", "c1", "s1"], "outcomes come back in input order")
check(outcomes[0].annotation and outcomes[2].annotation and outcomes[1].error is not None
      and isinstance(outcomes[1].error, anthropic.RateLimitError), "one chain failing does not sink the others")
check(Selective.peak >= 2, "chains are annotated in parallel")
check(any("could not be annotated" in m for m in messages) and sum(m.endswith(" annotated.") and "could not" not in m for m in messages) == 2,
      "progress reports each chain")
check(annotate_chains([], SETTINGS, print) == [], "no chains, no calls")
check(not any(MARKER in m for m in messages), "progress carries no document text")

print(f"\nALL {passed} CHECKS PASSED")
