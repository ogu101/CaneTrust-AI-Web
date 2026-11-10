"""
The analyze_documents tool: one annotation of one chain (docs/DESIGN.md, "Chains" and
"Workbook"). A chain is a base instrument (an original trust, the latest restatement, or a will)
plus the instruments that amend it, in date order. A chain with no amendments is simply n = 0.

Every field is a FieldHistory:

    original   -> what the BASE instrument says                 (template cols B-E)
    amendments -> one entry per amendment or codicil, in order   (template cols G-N)
    operative  -> the field after ALL of them; null when n = 0   (template cols P-S)

`original` comes before `operative` on purpose: the model works through the history before it
commits to a final answer.

The WHAT-to-extract instructions live on the single-instrument models below (BequestLanguage,
IncomeBeneficiaries, RestrictionsContingencies, DefinedTerm, UMMention) and are copied onto the
history models, so a field and its instruction never drift apart. The workbook's row anchors
(workbook.FIXED_ROWS) and the canned answers in testkit.py follow these field names.

Strict mode: this schema is NOT sent with strict: true. The API rejects it ("compiled grammar is
too large", tools/probe_schemas.py, docs/PROGRESS.md D0.1). Code validates instead: pydantic
here, reconcile() for the per-amendment entries, and annotate.check_citations() for page labels.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, create_model, field_validator

from schema_limits import StrictModel

STATUS_VALUES = ("Found", "Not in this document", "Could not determine", "Ambiguous")
EFFECT_VALUES = ("No change", "Changed", "Added", "Removed", "Ambiguous", "Could not determine")
FORM_VALUES = ("changes_only", "restating", "unclear")

Status = Literal["Found", "Not in this document", "Could not determine", "Ambiguous"]
Effect = Literal["No change", "Changed", "Added", "Removed", "Ambiguous", "Could not determine"]
Form = Literal["changes_only", "restating", "unclear"]


def canonical(value, allowed):
    """Enum capitalization is not guaranteed ('Not In This Document' can come back). Normalize
    case before the Literal check so a harmless casing difference can't fail validation."""
    if isinstance(value, str):
        lookup = {a.lower(): a for a in allowed}
        return lookup.get(value.strip().lower(), value)
    return value


STATUS_DESCRIPTION = (
    "Found: the instrument answers this; put the answer in `answer`, its location in `citation`. "
    "Not in this document: the instrument genuinely does not address this; a real finding, not a "
    "gap. Could not determine: it may be here but you could not establish it (unreadable page, a "
    "missing referenced instrument, unclear drafting); say why in `note`. Ambiguous: more than one "
    "defensible reading remains. Put both readings in `answer`, cite both locations in `citation`, "
    "and in `note` state exactly what conflicts, which provisions are involved, whether one is "
    "later in date, whether it expressly revokes the earlier one, and why it isn't resolved. Never "
    "choose one reading silently."
)


class Finding(StrictModel):
    status: Status = Field(description=STATUS_DESCRIPTION)
    answer: Optional[str] = Field(default=None, description=(
        "The finding itself. Quote verbatim where the field calls for exact language; otherwise "
        "state plainly. Null if status is not 'Found' or 'Ambiguous'."))
    citation: Optional[str] = Field(default=None, description=(
        "Instrument tag, article / section, and page, e.g. 'Trust, Art. 11, §1, p. 2' or "
        "'Amendment 2, §3, p. 4'. Pages are the labels in the [Page ...] markers, written "
        "'p. <label>'. Cite every location involved if status is 'Ambiguous'."))
    note: Optional[str] = Field(default=None, description=(
        "Judgment call, conflict explanation, or reason a determination could not be made. "
        "Required if status is 'Could not determine' or 'Ambiguous'."))

    @field_validator("status", mode="before")
    @classmethod
    def _status_case(cls, v):
        return canonical(v, STATUS_VALUES)


class UncategorizedFinding(StrictModel):
    text: str = Field(description=(
        "A material fact that fits no field above but that a reviewing attorney should see: a "
        "revocation of an earlier amendment, a pour-over to a trust, a postponement-of-distribution "
        "mechanism, a power of appointment, a no-contest clause."))
    citation: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None, description="Why this matters / what it does.")


# ---------------------------------------------------------------------------
# What to extract, one instrument at a time (instructions only; copied onto the histories)
# ---------------------------------------------------------------------------
class BequestLanguage(StrictModel):
    location: Finding = Field(description=(
        "Article/section/page where the bequest language appears: the language that disposes of "
        "property to a beneficiary. Common forms: 'the Trustor bequeaths', 'our Trustee shall "
        "divide the balance as follows', 'I give/devise', 'I bequeath', 'my Trustee shall "
        "distribute', 'the Trustee shall hold in trust for'. If the instrument uses none of these "
        "but still disposes of property to UM, use that language anyway and say so in `note`."))
    verbatim_text: Finding = Field(description=(
        "The complete bequest language, quoted exactly as written. No paraphrase."))
    plain_english_summary: Finding = Field(description=(
        "In plain English: what does the grantor or testator intend the University to receive? "
        "State whether the gift is a specific dollar amount, a percentage, specific property, "
        "income from the trust, part or all of the residue, or the remainder after other "
        "distributions."))
    trustee_executor_names: Finding = Field(description=(
        "Currently serving trustee(s); successor trustees in order of service; executor or "
        "personal representative if identified."))
    trustee_executor_contact: Finding = Field(description=(
        "Address, phone, and/or email for each person or institution named above, if the "
        "instrument provides it. If only the drafting attorney's contact info appears, say so; "
        "that is not the trustee's contact info."))
    um_receives: Finding = Field(description=(
        "Everything the University is designated to receive, combined. If a percentage, state "
        "whether it's of the gross estate, net estate, residue, or another defined fund. Note the "
        "exact name/school/department/fund used."))
    trigger_event: Finding = Field(description=(
        "What event causes UM's interest to become payable: must the grantor die first, must "
        "another beneficiary die first, are there multiple stages, does the trustee have "
        "discretion to delay, must debts/taxes/other gifts be paid first."))


class IncomeBeneficiaries(StrictModel):
    count: Finding = Field(description=(
        "How many income or lifetime beneficiaries are named. Use status 'Not in this document' "
        "if there are none."))
    names_and_relationship: Finding = Field(description=(
        "Full name of each income beneficiary, and their stated relationship to the grantor if "
        "given."))
    timing_implication: Finding = Field(description=(
        "What each beneficiary's interest implies for when the University receives its gift: "
        "does the interest last for life, a fixed period, or until another event?"))


class RestrictionsContingencies(StrictModel):
    primary_or_contingent: Finding = Field(description=(
        "Is the University a primary beneficiary or only a contingent one?"))
    conditions_precedent: Finding = Field(description=(
        "What must happen before UM becomes entitled to receive the gift? Could another "
        "beneficiary receive the property instead? Is the gift subject to a survival requirement?"))
    other_restrictions: Finding = Field(description=(
        "Other restrictions/limitations: conditions on UM's name, tax-exempt status, merger, or "
        "continued existence; restriction to a specific purpose/school/program/fund; an alternate "
        "charitable beneficiary if the purpose becomes impossible; trustee discretion to "
        "redirect/reduce/postpone/eliminate the gift; any conflict between the base instrument "
        "and later amendments."))


DEFINED_TERM_DESCRIPTION = (
    "The defined term itself, e.g. 'Trust Estate', 'Residuary Trust', 'Net Income', 'Descendants', "
    "'Charitable Beneficiary'.")
DEFINED_TERM_FINDING = (
    "Where it's defined and its exact definition as stated. In `note`, say whether the definition "
    "changes the amount, timing, priority, or conditions of UM's gift, and whether it's defined in "
    "another document that must be obtained.")
MENTION_DESCRIPTION = (
    "The exact name used for the University in this occurrence, plus enough surrounding text to "
    "show context (the sentence or clause it appears in). Include named schools, colleges, "
    "departments, or funds.")


# ---------------------------------------------------------------------------
# Histories
# ---------------------------------------------------------------------------
class AmendmentEntry(StrictModel):
    amendment_number: int = Field(description=(
        "The number of the amendment (or codicil) this entry is about, exactly as numbered in the "
        "chain (1 = the first one after the base instrument)."))
    effect: Effect = Field(description=(
        "What this amendment does to this field, measured against the field as it stood AFTER all "
        "earlier amendments (not against the base instrument). 'No change': the amendment does "
        "not alter it. 'Changed': it modifies or replaces what was there. 'Added': it introduces "
        "something the field did not have. 'Removed': it takes something away. 'Ambiguous': it may "
        "or may not affect the field and more than one reading is defensible. 'Could not "
        "determine': it may affect the field but you cannot establish how."))
    description: str = Field(description=(
        "What the amendment did and what the field now reads. Quote verbatim where the field calls "
        "for exact language. Empty string if effect is 'No change'. For 'Ambiguous', give every "
        "reading."))
    citation: str = Field(description=(
        "Article / section / page in THIS amendment, e.g. 'Amendment 2, §3, p. 4'. Empty string if "
        "effect is 'No change'."))

    @field_validator("effect", mode="before")
    @classmethod
    def _effect_case(cls, v):
        return canonical(v, EFFECT_VALUES)


class FieldHistory(StrictModel):
    original: Finding = Field(description=(
        "The field as the BASE instrument states it (the original or restated trust, or the will). "
        "Use status 'Not in this document' if the base instrument does not address it, including "
        "when the field is first introduced by an amendment."))
    amendments: list[AmendmentEntry] = Field(description=(
        "EXACTLY one entry for every amendment in the chain, in ascending amendment_number order, "
        "for every field, every time. Never skip an amendment that does not touch the field: write "
        "effect 'No change'. An empty list when the chain has no amendments."))
    operative: Optional[Finding] = Field(default=None, description=(
        "Null when the chain has no amendments. Otherwise the field's final position after "
        "applying all amendments in order, skipping any that was revoked. `answer` is the current "
        "position; `citation` names the CONTROLLING provision and its instrument (e.g. "
        "'Amendment 2, §3, p. 4' or 'Trust, Art. IV §2(b), p. 11'). If provisions conflict, or the "
        "chain is incomplete in a way that could change the answer, use 'Ambiguous' or 'Could not "
        "determine' and explain in `note`; never present a provisional answer as settled."))


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
    term: str = Field(description=DEFINED_TERM_DESCRIPTION)
    history: FieldHistory = Field(description=(
        DEFINED_TERM_FINDING + " `original` is how the base instrument defines the term ('Not in "
        "this document' if an amendment is the first to define it). An amendment that redefines, "
        "adds or deletes the term gets 'Changed' / 'Added' / 'Removed'."))


class InstrumentInfo(StrictModel):
    amendment_number: int = Field(description="The amendment's number, as in the chain.")
    form: Form = Field(description=(
        "'changes_only': states just what it alters (e.g. 'Article Six, Section 2 is amended to "
        "read...'), so anything it does not mention is unchanged. 'restating': repeats the whole "
        "instrument, so it must be compared against the prior state field by field. 'unclear': you "
        "cannot tell which; say why in `note` and treat 'No change' calls on it with care."))
    revokes: list[int] = Field(description=(
        "Numbers of earlier amendments this amendment revokes IN THEIR ENTIRETY. Empty list if "
        "none. A partial revocation is not listed here: describe it in `note` and in the affected "
        "fields."))
    note: str = Field(description=(
        "How you classified the form and what it revokes, with a citation, e.g. 'Revokes "
        "Amendment 1 in its entirety (Amendment 3, Preamble, p. 1)'. Empty string if nothing to add."))

    @field_validator("form", mode="before")
    @classmethod
    def _form_case(cls, v):
        return canonical(v, FORM_VALUES)


class ChainAnnotation(StrictModel):
    document_name: str = Field(description="The chain's name as given in the request.")
    instruments: list[InstrumentInfo] = Field(description=(
        "One entry per amendment in the chain (the base instrument needs none), ascending by "
        "amendment_number; empty when there are no amendments. Fill this in FIRST."))
    bequest_language: BequestLanguageHistory
    income_beneficiaries: IncomeBeneficiariesHistory
    restrictions: RestrictionsContingenciesHistory
    defined_terms: list[DefinedTermHistory] = Field(description=(
        "One entry per formally defined term that affects UM's interest, whether the base "
        "instrument or an amendment defines it. Only emit terms you actually found."))
    um_mentions: list[FieldHistory] = Field(description=(
        "One entry per distinct occurrence of the University's name (or a variant, including named "
        "schools/units/funds), across every instrument in the chain. " + MENTION_DESCRIPTION +
        " Put the base instrument's wording in `original.answer` (status 'Not in this document' if "
        "the mention first appears in an amendment, which then gets 'Added'), and the mention as "
        "it finally stands in `operative.answer`. An empty list is fine."))
    found_but_uncategorized: list[UncategorizedFinding] = Field(description=(
        "Material facts that fit no field above: revocations, pour-overs, court-ordered "
        "modifications and similar instrument-level events. An empty list is fine."))
    judgment_calls: list[str] = Field(description=(
        "Every point where you had to decide rather than read. An empty list is fine."))
    questions_for_client: list[str] = Field(description=(
        "Questions only the Office of Estate and Gift Planning can answer, each a direct, "
        "answerable question. If the chain is incomplete, ask for the missing instruments here."))


SECTIONS = ("bequest_language", "income_beneficiaries", "restrictions")


# ---------------------------------------------------------------------------
# Post-validation: make the output line up with the chain
# ---------------------------------------------------------------------------
def iter_histories(annotation: ChainAnnotation):
    """Yield (path, FieldHistory) for every field in the annotation."""
    for section in SECTIONS:
        obj = getattr(annotation, section)
        for fname in type(obj).model_fields:
            yield f"{section}.{fname}", getattr(obj, fname)
    for i, term in enumerate(annotation.defined_terms):
        yield f"defined_terms[{i}]", term.history
    for i, mention in enumerate(annotation.um_mentions):
        yield f"um_mentions[{i}]", mention


def reconcile(annotation: ChainAnnotation, expected_numbers: list[int]) -> list[str]:
    """Validation guarantees the SHAPE of the output, not that the model wrote exactly one entry
    per amendment. Put every `amendments` list in chain order and fill any skipped entry with a
    visible 'Could not determine' (never a silent blank). With no amendments, `operative` is the
    `original`; with amendments, a missing `operative` becomes 'Could not determine'.
    Returns warnings naming field paths and numbers only."""
    warnings: list[str] = []

    by_num = {info.amendment_number: info for info in annotation.instruments}
    instruments = []
    for n in expected_numbers:
        info = by_num.get(n)
        if info is None:
            warnings.append(f"instruments: model returned no entry for amendment {n}")
            info = InstrumentInfo(amendment_number=n, form="unclear", revokes=[],
                                  note="The model returned no classification for this amendment.")
        instruments.append(info)
    for n in sorted(set(by_num) - set(expected_numbers)):
        warnings.append(f"instruments: ignored entry for unexpected amendment number {n}")
    annotation.instruments = instruments

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
        if not expected_numbers:
            history.operative = history.original.model_copy()
        elif history.operative is None:
            warnings.append(f"{path}: model returned no operative position")
            history.operative = Finding(status="Could not determine",
                                        note="The model returned no operative position for this field.")
    return warnings
