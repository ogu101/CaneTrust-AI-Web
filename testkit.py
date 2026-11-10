"""
Canned model outputs for the API-free tests and the stub server. Pure builders plus one stub
client; nothing here touches disk or the network.

Importing this module also makes the tests safe to run: .env is never read, and any model call
that a test forgot to stub raises RealModelCallInTests instead of spending money.

    StubModel()          a stand-in for anthropic.Anthropic: answers record_triage, record_facts
                         and analyze_documents from the request itself (or from overrides), and
                         records every request. Install it with install_stub(service).
    chain_annotation()   a fictional ChainAnnotation for a chain with the given amendment numbers.
"""

import contextlib
import os
import re
import threading
import time
from datetime import date
from types import SimpleNamespace

os.environ["CANETRUST_DISABLE_DOTENV"] = "1"

import anthropic


class RealModelCallInTests(AssertionError):
    pass


class _BlockedAnthropic:
    def __init__(self, *args, **kwargs):
        raise RealModelCallInTests(
            "A real Anthropic client was created during tests or the stub server. "
            "Install testkit.StubModel (install_stub) instead.")


anthropic.Anthropic = _BlockedAnthropic

from annotation_schema import (  # noqa: E402
    AmendmentEntry, BequestLanguage, BequestLanguageHistory, ChainAnnotation, DefinedTermHistory,
    FieldHistory, Finding, IncomeBeneficiaries, IncomeBeneficiariesHistory, InstrumentInfo,
    RestrictionsContingencies, RestrictionsContingenciesHistory, UncategorizedFinding,
)

# Strict-mode schema checks live in schema_limits.py (no anthropic import), re-exported here.
from schema_limits import strict_schema_counts, strict_schema_problems  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Canned annotations
# ---------------------------------------------------------------------------
def hist(original, amendments=None, operative=None, numbers=(1, 2, 3), base="Trust", word="Amendment"):
    """amendments: {number: (effect, description, citation)}; the rest are 'No change'.
    With no numbers, operative is None (the n = 0 case)."""
    amendments = amendments or {}
    entries = []
    for number in numbers:
        effect, desc, cite = amendments.get(number, ("No change", "", ""))
        entries.append(AmendmentEntry(amendment_number=number, effect=effect, description=desc, citation=cite))
    if numbers:
        operative = operative or Finding(status="Found", answer=original, citation=f"{base}, Art. 1, p. 1")
    else:
        operative = None
    return FieldHistory(original=Finding(status="Found", answer=original, citation=f"{base}, Art. 1, p. 1"),
                        amendments=entries, operative=operative)


def chain_annotation(numbers=(1, 2, 3), n_terms=3, n_mentions=3, n_uncat=1, n_judgment=1, n_questions=1,
                     base="Trust", word="Amendment", name="TEST — Fictional chain"):
    """A fictional annotation. With numbers (1, 2, 3) it carries the classic test content:
    Amendment 2 changes um_receives, Amendment 3 makes trigger_event ambiguous, Amendment 1
    adds a restriction and is revoked by Amendment 2. Citations name page 1 of each instrument,
    so they pass check_citations for any instrument with a page labelled 1."""
    numbers = tuple(numbers)
    h = lambda original, amendments=None, operative=None: hist(original, amendments, operative, numbers, base, word)

    def section(model_cls):
        return {name: h(f"original {name}") for name in model_cls.model_fields}

    bequest = section(BequestLanguage)
    restrictions = section(RestrictionsContingencies)
    if 2 in numbers:
        bequest["um_receives"] = h(
            "25% of the residue", {2: ("Changed", "now 10% of the residue", f"{word} 2, §3, p. 1")},
            Finding(status="Found", answer="10% of the residue", citation=f"{word} 2, §3, p. 1"))
    if 3 in numbers:
        bequest["trigger_event"] = h(
            "Death of the grantor",
            {3: ("Ambiguous", "may also require the death of the spouse; or may not", f"{word} 3, §1, p. 1")},
            Finding(status="Ambiguous", answer="Death of the grantor, or of the grantor and spouse",
                    citation=f"{base}, Art. 1, p. 1; {word} 3, §1, p. 1",
                    note=f"{word} 3 does not say whether it replaces Art. 1."))
    if 1 in numbers:
        restrictions["other_restrictions"] = h(
            "Gift is restricted to the scholarship fund",
            {1: ("Added", "must also remain tax-exempt", f"{word} 1, Art. 2, p. 1")},
            Finding(status="Found", answer="Scholarship fund; University must remain tax-exempt",
                    citation=f"{word} 1, Art. 2, p. 1"))
    instruments = []
    for n in numbers:
        if n == 2:
            instruments.append(InstrumentInfo(amendment_number=2, form="restating",
                                              revokes=[1] if 1 in numbers else [],
                                              note=f"Revokes {word} 1 in its entirety ({word} 2, p. 1)"))
        else:
            instruments.append(InstrumentInfo(amendment_number=n, form="changes_only", revokes=[], note=""))
    return ChainAnnotation(
        document_name=name, instruments=instruments,
        bequest_language=BequestLanguageHistory(**bequest),
        income_beneficiaries=IncomeBeneficiariesHistory(**section(IncomeBeneficiaries)),
        restrictions=RestrictionsContingenciesHistory(**restrictions),
        defined_terms=[DefinedTermHistory(term=f"Term {i + 1}", history=h(f"definition {i + 1}"))
                       for i in range(n_terms)],
        um_mentions=[h(f"mention text {i + 1}") for i in range(n_mentions)],
        found_but_uncategorized=[UncategorizedFinding(text=f"uncategorized {i + 1}", citation=f"{base}, p. 1",
                                                      note="why it matters") for i in range(n_uncat)],
        judgment_calls=[f"judgment call {i + 1}" for i in range(n_judgment)],
        questions_for_client=[f"question {i + 1}?" for i in range(n_questions)],
    )


# ---------------------------------------------------------------------------
# Stub answers derived from the request
# ---------------------------------------------------------------------------
_SHEET_RE = re.compile(r"^\[(.+) sheet (\d+)\]$", re.M)
_DATES_RE = re.compile(r"dates=([^|\n]+)")
_MONTH_DATE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|"
                         r"November|December)\s+(\d{1,2}),\s+(\d{4})")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
STUB_PRINCIPAL = "Stub Principal"


def guess_type(file_id: str) -> str:
    name = file_id.lower()
    for word, doc_type in (("restat", "restated_trust"), ("codicil", "codicil"), ("will", "will"),
                           ("amend", "amendment"), ("notice", "notice_of_administration"),
                           ("letter", "letter"), ("account", "accounting")):
        if word in name:
            return doc_type
    return "trust"


def triage_answer(request) -> dict:
    """One document per file covering every sheet shown; the type from the file name; the date
    the first date on the file's first sheet. Good enough for the stub server and API tests."""
    message = request["messages"][0]["content"].split("\nYour previous answer")[0]
    shown: dict[str, list[int]] = {}
    first_dates: dict[str, str] = {}
    for block in message.split("\n\n"):
        m = _SHEET_RE.search(block)
        if not m:
            continue
        file_id, sheet = m.group(1), int(m.group(2))
        shown.setdefault(file_id, []).append(sheet)
        if file_id not in first_dates and (d := _DATES_RE.search(block)):
            found = _MONTH_DATE.search(d.group(1))
            if found:
                first_dates[file_id] = date(int(found[3]), MONTHS.index(found[1]) + 1, int(found[2])).isoformat()
    from triage import load_treatments
    from settings import load_settings
    table = load_treatments(load_settings().default_treatments)
    docs = []
    for file_id, sheets in shown.items():
        doc_type = guess_type(file_id)
        docs.append({
            "file_id": file_id, "first_sheet": min(sheets), "last_sheet": max(sheets), "doc_type": doc_type,
            "title": re.sub(r"\.\w+$", "", file_id).replace("_", " "), "principal": STUB_PRINCIPAL,
            "date": first_dates.get(file_id, "unknown"), "refers_to": [], "revokes": [],
            "appears_to_gift_to_university": "unclear", "is_blank_form": False,
            "proposed_treatment": table[doc_type], "treatment_reason": "stub", "confidence": "high",
            "evidence": [],
        })
    return {"documents": docs}


_NUMBERS_RE = re.compile(r"Amendment numbers to report on, exactly: \[([\d, ]*)\]")
_FAMILY_RE = re.compile(r"^Document family: (\w+)$", re.M)


def annotation_answer(request) -> dict:
    message = request["messages"][0]["content"]
    m = _NUMBERS_RE.search(message)
    numbers = tuple(int(x) for x in m.group(1).split(",") if x.strip()) if m else ()
    family = (_FAMILY_RE.search(message) or [None, "trust"])[1]
    base, word = {"trust": ("Trust", "Amendment"), "will": ("Will", "Codicil")}.get(family, ("Instrument", "Amendment"))
    return chain_annotation(numbers, n_terms=2, n_mentions=2, base=base, word=word).model_dump(mode="json")


def facts_answer(request) -> dict:
    return {"facts": []}


class StubModel:
    """A stand-in for anthropic.Anthropic. answers: {tool name: dict | callable(request) | None
    | Exception}. Missing tools use the defaults above. `pause` seconds before each answer."""

    DEFAULTS = {"record_triage": triage_answer, "record_facts": facts_answer,
                "analyze_documents": annotation_answer}

    def __init__(self, answers=None, pause=0.0):
        self.answers = dict(answers or {})
        self.pause = pause
        self.requests = []
        self.lock = threading.Lock()
        self.messages = self

    def calls(self, tool):
        return [r for r in self.requests if r["tools"][0]["name"] == tool]

    def stream(self, **request):
        with self.lock:
            self.requests.append(request)
        tool = request["tools"][0]["name"]
        answer = self.answers.get(tool, self.DEFAULTS.get(tool))
        if self.pause:
            time.sleep(self.pause)
        if isinstance(answer, BaseException):
            raise answer
        raw = answer(request) if callable(answer) else answer
        content = [] if raw is None else [SimpleNamespace(type="tool_use", name=tool, input=raw)]
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        message = SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)

        @contextlib.contextmanager
        def ctx():
            yield SimpleNamespace(get_final_message=lambda: message)
        return ctx()


def install_stub(service_module, stub=None):
    """Route every model call the service makes to `stub` (a new StubModel by default)."""
    stub = stub or StubModel()
    service_module.model_client = lambda settings: stub
    return stub


# ---------------------------------------------------------------------------
# Tally formulas, computed without a spreadsheet app
# ---------------------------------------------------------------------------
_TERM_RE = re.compile(r'([+-]?)(COUNTIF|COUNTBLANK)\(([A-Z]+)(\d+):([A-Z]+)(\d+)(?:,"([^"]*)")?\)')


def eval_tally(ws, formula: str) -> int:
    """Evaluate the workbook's tally formulas (sums of COUNTIF / COUNTBLANK over ranges in one
    column), with Excel's rules: '*' matches any non-empty text, 'x*' a prefix, otherwise a
    case-insensitive exact match; COUNTBLANK counts empty cells."""
    assert formula.startswith("="), formula
    body = formula[1:]
    terms = list(_TERM_RE.finditer(body))
    assert "".join(t.group(0) for t in terms) == body, f"unsupported formula: {formula}"
    total = 0
    for t in terms:
        sign, func, col, lo, col2, hi, crit = t.groups()
        assert col == col2
        values = [ws[f"{col}{r}"].value for r in range(int(lo), int(hi) + 1)]
        if func == "COUNTBLANK":
            n = sum(v in (None, "") for v in values)
        elif crit == "*":
            n = sum(isinstance(v, str) and v != "" for v in values)
        elif crit.endswith("*"):
            n = sum(isinstance(v, str) and v.lower().startswith(crit[:-1].lower()) for v in values)
        else:
            n = sum(isinstance(v, str) and v.lower() == crit.lower() for v in values)
        total += -n if sign == "-" else n
    return total
