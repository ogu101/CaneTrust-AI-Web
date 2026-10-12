"""
Structured output schema for packets WITH amendments.

Every field of the single-document annotation becomes a FieldHistory:

    original   -> what the original trust says            (template cols B-E)
    amendments -> one entry per amendment, in order        (template cols G-N)
    operative  -> what the field says after ALL of them    (template cols P-S)

`original` comes before `operative` on purpose: the model works through the
history before it commits to a final answer.

The WHAT-to-extract instructions are not repeated here. They are read off the
single-document schema (schema.py), so the two pipelines can't drift apart.

Strict tool use limits (24 optional params, 16 union-typed params in total):
everything in this file is required. The only optional/union parameters in the
whole packet schema are the ones inside Finding and UncategorizedFinding
(schema.py), which the single-document pipeline already uses.
"""

from typing import Literal

from pydantic import Field, create_model, field_validator

from schema import (
    StrictModel,
    Finding as _SingleFinding,
    UncategorizedFinding,
    BequestLanguage,
    IncomeBeneficiaries,
    RestrictionsContingencies,
    DefinedTerm,
    UMMention,
)

STATUS_VALUES = ("Found", "Not in this document", "Could not determine", "Ambiguous")
EFFECT_VALUES = ("No change", "Changed", "Added", "Removed", "Ambiguous", "Could not determine")
FORM_VALUES = ("changes_only", "restating", "unclear")

Effect = Literal["No change", "Changed", "Added", "Removed", "Ambiguous", "Could not determine"]
Form = Literal["changes_only", "restating", "unclear"]


def _canonical(value, allowed):
    """Strict mode does not guarantee enum capitalization ('Not In This Document'
    can come back). Normalize case before the Literal check so a harmless
    casing difference can't fail validation."""
    if isinstance(value, str):
        lookup = {a.lower(): a for a in allowed}
        return lookup.get(value.strip().lower(), value)
    return value


class Finding(_SingleFinding):
    """Same fields as the single-document Finding, plus case-insensitive status."""

    @field_validator("status", mode="before")
    @classmethod
    def _status_case(cls, v):
        return _canonical(v, STATUS_VALUES)


class AmendmentEntry(StrictModel):
    amendment_number: int = Field(description=
        "The number of the amendment this entry is about, exactly as numbered in "
        "the packet (1 = First Amendment).")
    effect: Effect = Field(description=
        "What this amendment does to this field, measured against the field as it "
        "stood AFTER all earlier amendments (not against the original trust). "
        "'No change': the amendment does not alter it. 'Changed': it modifies or "
        "replaces what was there. 'Added': it introduces something the field did "
        "not have. 'Removed': it takes something away. 'Ambiguous': it may or may "
        "not affect the field and more than one reading is defensible. 'Could not "
        "determine': it may affect the field but you cannot establish how "
        "(unreadable text, a missing instrument it refers to).")
    description: str = Field(description=
        "What the amendment did and what the field now reads. Quote verbatim "
        "where the field calls for exact language. Empty string if effect is "
        "'No change'. For 'Ambiguous', give every reading.")
    citation: str = Field(description=
        "Article / section / page in THIS amendment. Empty string if effect is "
        "'No change'.")

    @field_validator("effect", mode="before")
    @classmethod
    def _effect_case(cls, v):
        return _canonical(v, EFFECT_VALUES)


class FieldHistory(StrictModel):
    original: Finding = Field(description=
        "The field as the ORIGINAL TRUST states it, filled in exactly as in a "
        "single-document annotation (status, answer, citation, note). Use status "
        "'Not in this document' if the original trust does not address it, "
        "including when the field is first introduced by an amendment.")
    amendments: list[AmendmentEntry] = Field(description=
        "EXACTLY one entry for every amendment in the packet, in ascending "
        "amendment_number order, for every field, every time. Never skip an "
        "amendment that does not touch the field: write effect 'No change'.")
    operative: Finding = Field(description=
        "The field's final position after applying all amendments in order, "
        "skipping any amendment that was revoked. `answer` is the current "
        "position; `citation` names the CONTROLLING provision, including which "
        "instrument it is in (e.g. 'Second Amendment, §3, p. 4' or 'Original "
        "Trust, Art. IV §2(b), p. 11'). If provisions conflict, or the chain is "
        "incomplete in a way that could change the answer, use 'Ambiguous' or "
        "'Could not determine' and explain in `note`; never present a provisional "
        "answer as settled.")


def _history_version(name: str, single_model: type[StrictModel]) -> type[StrictModel]:
    """Same fields, same field instructions, each one now a FieldHistory."""
    fields = {
        fname: (FieldHistory, Field(description=finfo.description))
        for fname, finfo in single_model.model_fields.items()
    }
    return create_model(name, __base__=StrictModel, __doc__=single_model.__doc__, **fields)


BequestLanguageHistory = _history_version("BequestLanguageHistory", BequestLanguage)
IncomeBeneficiariesHistory = _history_version("IncomeBeneficiariesHistory", IncomeBeneficiaries)
RestrictionsContingenciesHistory = _history_version(
    "RestrictionsContingenciesHistory", RestrictionsContingencies)


class DefinedTermHistory(StrictModel):
    term: str = Field(description=DefinedTerm.model_fields["term"].description)
    history: FieldHistory = Field(description=
        DefinedTerm.model_fields["finding"].description +
        " `original` is how the original trust defines the term ('Not in this "
        "document' if an amendment is the first to define it). An amendment that "
        "redefines, adds or deletes the term gets 'Changed' / 'Added' / 'Removed'.")


class InstrumentInfo(StrictModel):
    amendment_number: int = Field(description="The amendment's number, as in the packet.")
    form: Form = Field(description=
        "'changes_only': states just what it alters (e.g. 'Article Six, Section 2 "
        "is amended to read...'), so anything it does not mention is unchanged. "
        "'restating': repeats the whole trust (an amended and restated instrument), "
        "so it must be compared against the prior state field by field. 'unclear': "
        "you cannot tell which; say why in `note` and treat 'No change' calls on "
        "it with care.")
    revokes: list[int] = Field(description=
        "Numbers of earlier amendments this amendment revokes IN THEIR ENTIRETY. "
        "Empty list if none. A partial revocation is not listed here: describe it "
        "in `note` and in the affected fields.")
    note: str = Field(description=
        "How you classified the form and what it revokes, with a citation, e.g. "
        "'Revokes the Fourth Amendment in its entirety (Preamble, p. 1)'. Empty "
        "string if there is nothing to add.")

    @field_validator("form", mode="before")
    @classmethod
    def _form_case(cls, v):
        return _canonical(v, FORM_VALUES)


class PacketAnnotation(StrictModel):
    document_name: str
    instruments: list[InstrumentInfo] = Field(description=
        "One entry per amendment in the packet (the original trust needs none), "
        "ascending by amendment_number. Fill this in FIRST: classify each "
        "amendment's form and note what it revokes before working through the fields.")
    bequest_language: BequestLanguageHistory
    income_beneficiaries: IncomeBeneficiariesHistory
    restrictions: RestrictionsContingenciesHistory
    defined_terms: list[DefinedTermHistory] = Field(description=
        "One entry per formally defined term that affects UM's interest, whether "
        "the original trust or an amendment defines it. Only emit terms you "
        "actually found; an empty list is fine.")
    um_mentions: list[FieldHistory] = Field(description=
        "One entry per distinct occurrence of the University's name (or a variant, "
        "including named schools/units/funds), across the original trust and all "
        "amendments. " + UMMention.model_fields["exact_text"].description +
        " Put the original trust's wording in `original.answer` (status 'Not in "
        "this document' if the mention first appears in an amendment, in which "
        "case describe it under that amendment's entry with 'Added'), and the "
        "mention as it finally stands in `operative.answer`. An empty list is fine.")
    found_but_uncategorized: list[UncategorizedFinding] = Field(description=
        "Material facts that fit no field above. In a packet this is where "
        "revocations of earlier amendments, court-ordered modifications and "
        "similar instrument-level events belong. An empty list is fine.")
    judgment_calls: list[str] = Field(description=
        "Every point where you had to decide rather than read: how you classified "
        "an amendment's form, which provision you treated as controlling, whether "
        "two differently named funds are the same gift. An empty list is fine.")
    questions_for_client: list[str] = Field(description=
        "Questions only the Office of Estate and Gift Planning can answer, each a "
        "direct, answerable question. If the chain is incomplete, ask for the "
        "missing amendments here. An empty list is fine.")


# ----------------------------------------------------------------------------
# Post-validation: make the amendment entries line up with the packet
# ----------------------------------------------------------------------------

def iter_histories(annotation: PacketAnnotation):
    """Yield (path, FieldHistory) for every field in the annotation."""
    for section in ("bequest_language", "income_beneficiaries", "restrictions"):
        obj = getattr(annotation, section)
        for fname in type(obj).model_fields:
            yield f"{section}.{fname}", getattr(obj, fname)
    for i, term in enumerate(annotation.defined_terms):
        yield f"defined_terms[{i}] ({term.term})", term.history
    for i, mention in enumerate(annotation.um_mentions):
        yield f"um_mentions[{i}]", mention


def reconcile_with_packet(annotation: PacketAnnotation, expected_numbers: list[int]) -> list[str]:
    """Strict mode guarantees the SHAPE of the output, not that the model wrote
    exactly one entry per amendment. Put every `amendments` list in packet order,
    and fill any entry the model skipped with a visible 'Could not determine'
    (never a silent blank). Returns human-readable warnings."""
    warnings: list[str] = []

    # instruments
    by_num = {info.amendment_number: info for info in annotation.instruments}
    instruments = []
    for n in expected_numbers:
        info = by_num.get(n)
        if info is None:
            warnings.append(f"instruments: model returned no entry for amendment {n}")
            info = InstrumentInfo(
                amendment_number=n, form="unclear", revokes=[],
                note="The model returned no classification for this amendment.")
        instruments.append(info)
    for n in set(by_num) - set(expected_numbers):
        warnings.append(f"instruments: ignored entry for unexpected amendment number {n}")
    annotation.instruments = instruments

    # every field's per-amendment entries
    for path, history in iter_histories(annotation):
        by_number: dict[int, AmendmentEntry] = {}
        for entry in history.amendments:
            by_number.setdefault(entry.amendment_number, entry)
        entries = []
        for n in expected_numbers:
            entry = by_number.get(n)
            if entry is None:
                warnings.append(f"{path}: model returned no entry for amendment {n}")
                entry = AmendmentEntry(
                    amendment_number=n, effect="Could not determine",
                    description="The model returned no entry for this amendment. "
                                "Check this field against the amendment manually.",
                    citation="")
            entries.append(entry)
        extra = set(by_number) - set(expected_numbers)
        if extra:
            warnings.append(f"{path}: ignored entries for unexpected amendment numbers {sorted(extra)}")
        history.amendments = entries

    return warnings
