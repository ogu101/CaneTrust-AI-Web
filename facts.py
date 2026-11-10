"""
Facts lane (docs/DESIGN.md, "Facts lane"): who is serving, how to reach them, and the court
facts, from any document in the packet.

    extract_facts(documents, settings, client=None) -> FactsResult

One small strict call to record_facts. The model sees the full text of every facts_only
document and, from every other document that is not excluded, only the pages that look like
contact sections (phone numbers, e-mail, ZIP codes, "successor trustee", "personal
representative", "attorney for", "date of death", "case no."). The model says which document
each fact came from; code fills in that document's date, so "newest" is the triage date, never
the model's opinion.

Merge rules (merge_facts):
  * facts are grouped by field and subject (the person or institution a contact belongs to,
    normalized like a principal); values are compared after normalizing case, spacing,
    punctuation and phone digits;
  * the newest source wins (a source with no date counts as oldest; ties keep file order, the
    later document winning), and every differing value is kept and the fact marked as a
    conflict: shown, not hidden;
  * facts never feed the bequest, income or restriction fields. They become contact rows only
    (see contact_rows()).

Never log or put into a warning any page text or any value the model returned. Warnings carry
ids, field names and counts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, ValidationError, field_validator

from settings import Settings, require_api_key
from triage import NO_FORCED_TOOL_CHOICE, normalize_principal
from triage_schema import StrictModel

TOOL_NAME = "record_facts"
MAX_CONTACT_PAGES = 8

FactField = Literal[
    "date_of_death", "probate_case_number", "probate_court",
    "personal_representative", "trustee", "successor_trustee", "backup_successor_trustee",
    "attorney", "attorney_firm", "address", "phone", "email",
]
FACT_FIELDS: tuple[str, ...] = get_args(FactField)
CONTACT_FIELDS = ("address", "phone", "email")

# Where each fact goes in the workbook: (row label, fields). Contact rows only.
CONTACT_ROWS = (
    ("Personal representative", ("personal_representative",)),
    ("Trustee / successor trustees", ("trustee", "successor_trustee", "backup_successor_trustee")),
    ("Date of death", ("date_of_death",)),
    ("Probate case", ("probate_case_number", "probate_court")),
    ("Attorney", ("attorney", "attorney_firm")),
)

_CUES = [
    re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"),
    re.compile(r"\b[A-Z]{2},?\s+\d{5}(?:-\d{4})?\b"),
    re.compile(r"successor\s+trustee|personal\s+representative|executor|attorneys?\s+for|"
               r"date\s+of\s+death|died\s+on|case\s+(?:no|number)", re.I),
]


class FactItem(StrictModel):
    field: FactField = Field(description="Which fact this is (see the list in the instructions).")
    subject: str = Field(description=(
        "For address, phone and email: the person or institution it belongs to, as named in the "
        "document. For every other field: 'unknown'."))
    value: str = Field(description=(
        "The fact as printed. Dates as YYYY-MM-DD. Names in full. One address, phone number or "
        "e-mail per item."))
    document_id: str = Field(description="The id of the document it was found in, e.g. 'd4'.")
    page: str = Field(description="The page label from the [Page ...] marker where it appears, or 'unknown'.")

    @field_validator("field", mode="before")
    @classmethod
    def _field_case(cls, v):
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "_").replace("-", "_")
            return key if key in FACT_FIELDS else v
        return v


class FactsAnswer(StrictModel):
    facts: list[FactItem] = Field(description=(
        "Every fact found, one item per value per document. Report the same fact again for "
        "each document that states it. Empty if none."))


def tool_definition() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Record the people, contacts and court facts found. Call this exactly once.",
        "input_schema": FactsAnswer.model_json_schema(),
        "strict": True,
        "cache_control": {"type": "ephemeral"},
    }


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Fact:
    field: str
    value: str
    subject: str | None                  # normalized-able name for contact fields, else None
    source_document: str
    source_date: date | None
    page: str | None = None


@dataclass
class MergedFact:
    field: str
    subject: str | None
    value: str                           # the preferred (newest) value
    sources: list[Fact]                  # every source, newest first
    conflict: bool = False

    @property
    def values(self) -> list[str]:
        """Distinct values, preferred first."""
        out: list[str] = []
        for f in self.sources:
            if _norm_value(self.field, f.value) not in {_norm_value(self.field, v) for v in out}:
                out.append(f.value)
        return out

    def to_dict(self) -> dict:
        return {"field": self.field, "subject": self.subject, "value": self.value,
                "conflict": self.conflict, "values": self.values,
                "sources": [{"document": f.source_document, "value": f.value, "page": f.page,
                             "date": f.source_date.isoformat() if f.source_date else None}
                            for f in self.sources]}


@dataclass
class FactsResult:
    facts: list[MergedFact]
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def get(self, field_name: str, subject: str | None = None) -> list[MergedFact]:
        key = normalize_principal(subject) if subject else None
        return [f for f in self.facts if f.field == field_name
                and (subject is None or (f.subject and normalize_principal(f.subject) == key))]

    def to_dict(self) -> dict:
        return {"facts": [f.to_dict() for f in self.facts], "warnings": list(self.warnings),
                "usage": dict(self.usage)}


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
def _norm_value(field_name: str, value: str) -> str:
    if field_name == "phone":
        digits = re.sub(r"\D", "", value)
        return digits[-10:] if len(digits) >= 10 else digits
    return " ".join(re.sub(r"[^\w@]+", " ", value.lower()).split())


def merge_facts(facts: list[Fact], order: dict[str, int] | None = None) -> list[MergedFact]:
    """Group by (field, subject); newest source first; differing values make a conflict.
    `order` (document id -> file-order position) breaks date ties: later document wins."""
    order = order or {}
    groups: dict[tuple, list[Fact]] = {}
    for f in facts:
        subject_key = normalize_principal(f.subject) if f.subject else None
        groups.setdefault((f.field, subject_key), []).append(f)
    merged = []
    for (field_name, _), items in groups.items():
        items = sorted(items, key=lambda f: (f.source_date or date.min, order.get(f.source_document, -1)),
                       reverse=True)
        distinct = {_norm_value(field_name, f.value) for f in items}
        merged.append(MergedFact(field_name, items[0].subject, items[0].value, items, len(distinct) > 1))
    rank = {name: i for i, name in enumerate(FACT_FIELDS)}
    merged.sort(key=lambda m: (rank[m.field], normalize_principal(m.subject or "") or ""))
    return merged


def contact_rows(result: FactsResult) -> list[dict]:
    """Rows for the workbook's contact block: label, answer, citation (sources with dates),
    note (conflicts). Contacts of a named person follow that person's row."""
    rows = []
    for label, fields in CONTACT_ROWS:
        names = [m for m in result.facts if m.field in fields]
        if not names:
            rows.append({"label": label, "answer": None, "citation": None,
                         "note": None, "status": "Not in this document"})
            continue
        answer, citation, notes = [], [], []
        for m in names:
            answer.append(f"{m.field.replace('_', ' ')}: {m.value}")
            if m.field in ("personal_representative", "trustee", "successor_trustee",
                           "backup_successor_trustee", "attorney"):
                for c in (x for x in result.facts if x.field in CONTACT_FIELDS and x.subject
                          and normalize_principal(x.subject) == normalize_principal(m.value)):
                    answer.append(f"  {c.field}: {c.value}")
                    citation.append(_cite(c))
                    if c.conflict:
                        notes.append(_conflict_note(c))
            citation.append(_cite(m))
            if m.conflict:
                notes.append(_conflict_note(m))
        rows.append(_row(label, answer, citation, notes))
    named = {normalize_principal(m.value) for m in result.facts
             if m.field in ("personal_representative", "trustee", "successor_trustee",
                            "backup_successor_trustee", "attorney")}
    others = [c for c in result.facts if c.field in CONTACT_FIELDS
              and not (c.subject and normalize_principal(c.subject) in named)]
    if others:
        rows.append(_row("Other contacts",
                         [f"{c.subject or 'unnamed'}, {c.field}: {c.value}" for c in others],
                         [_cite(c) for c in others], [_conflict_note(c) for c in others if c.conflict]))
    return rows


def _row(label, answer, citation, notes) -> dict:
    return {"label": label, "answer": "\n".join(answer), "citation": "; ".join(dict.fromkeys(citation)),
            "note": "\n".join(notes) or None, "status": "Ambiguous" if notes else "Found"}


def _cite(m: MergedFact) -> str:
    parts = []
    for f in m.sources:
        when = f.source_date.isoformat() if f.source_date else "undated"
        page = f", p. {f.page}" if f.page else ""
        parts.append(f"{f.source_document} ({when}{page})")
    return f"{m.field.replace('_', ' ')}: " + ", ".join(dict.fromkeys(parts))


def _conflict_note(m: MergedFact) -> str:
    shown = "; ".join(f"{f.value} [{f.source_document}, "
                      f"{f.source_date.isoformat() if f.source_date else 'undated'}]" for f in m.sources)
    return (f"Conflict in {m.field.replace('_', ' ')}"
            + (f" for {m.subject}" if m.subject else "")
            + f": newest source preferred. All values: {shown}")


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------
def _contact_pages(pages) -> list:
    scored = [(sum(len(r.findall(p.text or "")) for r in _CUES), i, p) for i, p in enumerate(pages)]
    picked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))[:MAX_CONTACT_PAGES]
    return [p for _, _, p in sorted(picked, key=lambda s: s[1])]


def build_message(documents, *, max_chars: int) -> tuple[str, list[str], list[str]]:
    """(user message, ids shown, warnings). Full text of facts_only documents; contact pages
    of the rest; excluded documents never."""
    blocks, shown, warnings, used = [], [], [], 0
    for d in documents:
        if getattr(d, "excluded", False):
            continue
        full = d.treatment == "facts_only"
        pages = list(d.pages) if full else _contact_pages(d.pages)
        if not pages:
            continue
        when = d.date.isoformat() if d.date else "undated"
        head = (f"=== Document {d.id} ({d.doc_type}, {when}, "
                f"{'full text' if full else 'contact pages only'}) ===")
        body = "\n\n".join(f"[Page {p.label}]\n{p.text}" for p in pages)
        if used + len(body) > max_chars:
            warnings.append(f"{d.id}: left out of the facts call, over the {max_chars}-character limit")
            continue
        used += len(body)
        blocks.append(f"{head}\n{body}")
        shown.append(d.id)
    message = (f"Documents shown: {', '.join(shown) or 'none'}.\n"
               f"Call {TOOL_NAME} exactly once.\n\n" + "\n\n".join(blocks))
    return message, shown, warnings


RULES = """You read estate-administration papers for the University of Miami Office of Estate and \
Gift Planning and record who is serving and how to reach them. Call record_facts exactly once.

Fields:
- date_of_death: the decedent's date of death, YYYY-MM-DD.
- probate_case_number, probate_court: the court case number and the court, as printed.
- personal_representative: the personal representative (executor) appointed or nominated.
- trustee, successor_trustee, backup_successor_trustee: the currently serving trustee, the \
successor trustee named first, and the next one named after that.
- attorney, attorney_firm: the attorney for the estate or trust, and the firm.
- address, phone, email: contact details. `subject` is the person or institution they belong \
to. A law firm's address is the attorney's, not the client's: never give the attorney's \
contact details as the representative's or trustee's.

Report a fact once for every document that states it, with that document's id and the page \
label from its [Page ...] marker. Copy values as printed; do not combine or correct them. \
Leave out anything you are not sure of. Report nothing about gifts, beneficiaries or \
restrictions."""


def build_request(settings: Settings, message: str) -> dict:
    forced = not settings.triage_model.startswith(NO_FORCED_TOOL_CHOICE)
    return dict(
        model=settings.triage_model,
        max_tokens=settings.facts_max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.triage_effort},
        system=[{"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}}],
        tools=[tool_definition()],
        tool_choice={"type": "tool", "name": TOOL_NAME} if forced else {"type": "auto"},
        messages=[{"role": "user", "content": message}],
    )


def parse_answer(raw, documents, shown: list[str]) -> tuple[list[Fact], list[str]]:
    """Validate item by item; keep what is valid. Warnings carry positions and ids only."""
    by_id = {d.id: d for d in documents}
    labels = {d.id: {p.label for p in d.pages} for d in documents}
    items = raw.get("facts") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], ["facts: the answer has no 'facts' list"]
    facts, warnings = [], []
    for index, item in enumerate(items, start=1):
        try:
            parsed = FactItem.model_validate(item)
        except ValidationError:
            warnings.append(f"facts: item {index} is invalid and was dropped")
            continue
        if parsed.document_id not in shown:
            warnings.append(f"facts: item {index} names a document that was not shown; dropped")
            continue
        if not parsed.value.strip() or parsed.value.strip().lower() == "unknown":
            continue
        page = parsed.page if parsed.page in labels[parsed.document_id] else None
        if parsed.page.lower() != "unknown" and page is None:
            warnings.append(f"facts: item {index} cites a page label not in {parsed.document_id}")
        subject = parsed.subject.strip() if parsed.field in CONTACT_FIELDS else None
        if subject is not None and subject.lower() == "unknown":
            subject = None
        doc = by_id[parsed.document_id]
        facts.append(Fact(parsed.field, parsed.value.strip(), subject, parsed.document_id, doc.date, page))
    return facts, warnings


def extract_facts(documents, settings: Settings, client=None) -> FactsResult:
    """documents: objects with id, doc_type, date, treatment, pages (PageRecords with labels),
    and optionally excluded. One call; an unusable answer gives no facts and a warning."""
    message, shown, warnings = build_message(documents, max_chars=settings.facts_max_chars)
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    if not shown:
        return FactsResult([], warnings + ["facts: no document had contact pages"], usage)
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=require_api_key(settings))
    with client.messages.stream(**build_request(settings, message)) as stream:
        response = stream.get_final_message()
    u = getattr(response, "usage", None)
    usage = {"calls": 1, "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
             "output_tokens": int(getattr(u, "output_tokens", 0) or 0)}
    blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"
              and getattr(b, "name", None) == TOOL_NAME]
    if not blocks:
        return FactsResult([], warnings + [f"facts: no {TOOL_NAME} call was made"], usage)
    raw = blocks[0].input
    if settings.debug_dir is not None:
        settings.debug_dir.mkdir(parents=True, exist_ok=True)
        (settings.debug_dir / "last_raw_facts.json").write_text(json.dumps(raw, indent=2))
    if response.stop_reason == "max_tokens":
        warnings.append("facts: the answer was cut off at max_tokens; some facts may be missing")
    facts, parse_warnings = parse_answer(raw, documents, shown)
    order = {d.id: i for i, d in enumerate(documents)}
    return FactsResult(merge_facts(facts, order), warnings + parse_warnings, usage)
