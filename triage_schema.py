"""
The record_triage tool: the model's answer for one triage call (docs/DESIGN.md, "Triage").

Strict tool use: every field is required, nothing is optional or a union, and "unknown" stands
in for null. Strict schemas also cannot carry maxItems / maxLength / minimum, so the limits on
evidence (3 strings, 12 words each) live in the descriptions and are enforced in code by
trimming (clean_evidence), not by rejecting the answer.

parse_documents() turns the raw tool input into TriageDocument objects document by document, so
one bad document does not sink the rest. Its problems name positions and field names only,
never a value the model returned: those values are document text.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, get_args

from pydantic import Field, ValidationError, field_validator

from schema import StrictModel

DocType = Literal[
    "restated_trust", "trust", "amendment", "will", "codicil", "other_dispositive",
    "accounting", "receipt_of_notice", "letter", "letters_of_administration",
    "notice_of_administration", "notice_to_creditors", "other_administrative", "unknown",
]
Treatment = Literal["annotate", "facts_only", "log_only"]
YesNoUnclear = Literal["yes", "no", "unclear"]
Confidence = Literal["high", "medium", "low"]

DOC_TYPES: tuple[str, ...] = get_args(DocType)
TREATMENTS: tuple[str, ...] = get_args(Treatment)
MAX_EVIDENCE = 3
MAX_EVIDENCE_WORDS = 12
TOOL_NAME = "record_triage"


def _canonical(value, allowed):
    """Strict mode does not guarantee enum capitalization; normalize case (and spaces or
    hyphens for underscores) before the Literal check."""
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "_").replace("-", "_")
        lookup = {a.lower(): a for a in allowed}
        return lookup.get(key, value)
    return value


class Reference(StrictModel):
    what: str = Field(description="The instrument referred to, briefly, e.g. 'revocable trust' or 'first amendment'.")
    date: str = Field(description="Its date as YYYY-MM-DD, or 'unknown'. Never guess a day.")


class TriageDocument(StrictModel):
    file_id: str = Field(description="The file_id from the page headers. A document never spans two files.")
    first_sheet: int = Field(description="First sheet of the document in that file (1-based).")
    last_sheet: int = Field(description="Last sheet of the document in that file, inclusive.")
    doc_type: DocType = Field(description="One type from the taxonomy in the instructions.")
    title: str = Field(description="Short title as the document itself states it, or 'unknown'.")
    principal: str = Field(description="The person whose trust or estate the document concerns, or 'unknown'.")
    date: str = Field(description=(
        "The document's own execution, signing or issue date, or the date a notice states for its "
        "own publication or filing, as YYYY-MM-DD. 'unknown' if absent or only partly legible."))
    refers_to: list[Reference] = Field(description="Instruments this document refers to (amends, restates, revokes, administers).")
    revokes: list[Reference] = Field(description=(
        "Instruments this document expressly revokes in their entirety (also list them in "
        "refers_to). Empty if none. A restatement replacing an earlier trust is not a revocation."))
    appears_to_gift_to_university: YesNoUnclear = Field(description=(
        "Whether this document itself makes a gift to the University of Miami. Being addressed "
        "or sent to the University is not a gift."))
    is_blank_form: bool = Field(description="True for an unsigned, unfilled copy of a form.")
    proposed_treatment: Treatment = Field(description="annotate, facts_only or log_only.")
    treatment_reason: str = Field(description="One short sentence.")
    confidence: Confidence = Field(description="Confidence in this document's boundaries and type.")
    evidence: list[str] = Field(description=f"At most {MAX_EVIDENCE} short cues, each at most {MAX_EVIDENCE_WORDS} words.")

    @field_validator("doc_type", mode="before")
    @classmethod
    def _doc_type_case(cls, v):
        return _canonical(v, DOC_TYPES)

    @field_validator("proposed_treatment", mode="before")
    @classmethod
    def _treatment_case(cls, v):
        return _canonical(v, TREATMENTS)

    @field_validator("appears_to_gift_to_university", mode="before")
    @classmethod
    def _gift_case(cls, v):
        return _canonical(v, get_args(YesNoUnclear))

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_case(cls, v):
        return _canonical(v, get_args(Confidence))


class TriageAnswer(StrictModel):
    documents: list[TriageDocument] = Field(description=(
        "Every document in the pages shown, in file order then sheet order. Every sheet belongs "
        "to exactly one document."))


def tool_definition() -> dict:
    return {
        "name": TOOL_NAME,
        "description": ("Record where each document starts and ends, and what it is. Call this "
                        "exactly once, covering every sheet shown."),
        "input_schema": TriageAnswer.model_json_schema(),
        "strict": True,
        "cache_control": {"type": "ephemeral"},
    }


def parse_date(value: str) -> tuple[date | None, bool]:
    """(parsed date, ok). 'unknown' is ok and None; anything unparseable is not ok."""
    text = value.strip() if isinstance(value, str) else ""
    if text.lower() == "unknown":
        return None, True
    try:
        return date.fromisoformat(text), len(text) == 10
    except ValueError:
        return None, False


def clean_evidence(items: list[str]) -> list[str]:
    return [" ".join(item.split()[:MAX_EVIDENCE_WORDS]) for item in items[:MAX_EVIDENCE]]


def _field_name(err) -> str:
    if err["type"] == "extra_forbidden":
        return "(unexpected field)"              # the key itself could be model text
    return ".".join(str(p) for p in err["loc"]) or "(document)"


def parse_documents(raw) -> tuple[list[tuple[int, TriageDocument, bool]], list[str]]:
    """Validate each raw document on its own, so one bad document does not sink the rest.

    Returns (index, document, type_was_invalid) triples and problems. A document whose only
    fault is its doc_type is kept with type "unknown" (type_was_invalid=True), so a repair can
    use it. Problems name positions and field names only. Documents are numbered from 1."""
    docs = raw.get("documents") if isinstance(raw, dict) else None
    if not isinstance(docs, list):
        return [], ["the answer has no 'documents' list"]
    parsed, problems = [], []
    for index, item in enumerate(docs, start=1):
        try:
            parsed.append((index, TriageDocument.model_validate(item), False))
            continue
        except ValidationError as exc:
            fields = sorted({_field_name(err) for err in exc.errors()})
        problems.append(f"document {index}: invalid or missing field(s) {', '.join(fields)}")
        if fields == ["doc_type"]:
            try:
                parsed.append((index, TriageDocument.model_validate({**item, "doc_type": "unknown"}), True))
            except ValidationError:
                pass
    return parsed, problems
