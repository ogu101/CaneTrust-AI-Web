"""
Canned model outputs for the API-free tests: a fictional packet annotation and a minimal
single-document annotation. Pure builders; nothing here touches disk or the network.
"""

from schema import (
    BequestLanguage, DefinedTerm, DocumentAnnotation, Finding as SingleFinding,
    IncomeBeneficiaries, RestrictionsContingencies, UMMention, UncategorizedFinding,
)
from schema_multi import (
    AmendmentEntry, BequestLanguageHistory, DefinedTermHistory, Finding, FieldHistory,
    IncomeBeneficiariesHistory, InstrumentInfo, PacketAnnotation,
    RestrictionsContingenciesHistory,
)


def hist(original, amendments=None, operative=None, n=3):
    """amendments: {number: (effect, description, citation)}; the rest are 'No change'."""
    amendments = amendments or {}
    entries = []
    for number in range(1, n + 1):
        effect, desc, cite = amendments.get(number, ("No change", "", ""))
        entries.append(AmendmentEntry(amendment_number=number, effect=effect,
                                      description=desc, citation=cite))
    return FieldHistory(
        original=Finding(status="Found", answer=original, citation="Art. 1, p. 1"),
        amendments=entries,
        operative=operative or Finding(status="Found", answer=original, citation="Original Trust, Art. 1, p. 1"),
    )


def make_packet_annotation(n_terms=3, n_mentions=3, n_uncat=1, n_judgment=1, n_questions=1):
    def section(model_cls):
        fields = {}
        for name in model_cls.model_fields:
            fields[name] = hist(f"original {name}")
        return fields

    bequest = section(BequestLanguage)
    bequest["um_receives"] = hist(
        "25% of the residue",
        {2: ("Changed", "now 10% of the residue", "Second Amendment, §3, p. 4")},
        Finding(status="Found", answer="10% of the residue",
                citation="Second Amendment, §3, p. 4"),
    )
    bequest["trigger_event"] = hist(
        "Death of the grantor",
        {3: ("Ambiguous", "may also require the death of the spouse; or may not", "Third Amendment, §1, p. 2")},
        Finding(status="Ambiguous", answer="Death of the grantor, or of the grantor and spouse",
                citation="Original Trust, Art. 1, p. 1; Third Amendment, §1, p. 2",
                note="Third Amendment does not say whether it replaces Art. 1."),
    )
    restrictions = section(RestrictionsContingencies)
    restrictions["other_restrictions"] = hist(
        "Gift is restricted to the scholarship fund",
        {1: ("Added", "must also remain tax-exempt", "First Amendment, Art. 2, p. 1")},
        Finding(status="Found", answer="Scholarship fund; University must remain tax-exempt",
                citation="First Amendment, Art. 2, p. 1"),
    )
    return PacketAnnotation(
        document_name="TEST — Fictional Trust with 3 Amendments",
        instruments=[
            InstrumentInfo(amendment_number=1, form="changes_only", revokes=[], note=""),
            InstrumentInfo(amendment_number=2, form="restating", revokes=[1],
                           note="Revokes the First Amendment in its entirety (Preamble, p. 1)"),
            InstrumentInfo(amendment_number=3, form="changes_only", revokes=[], note=""),
        ],
        bequest_language=BequestLanguageHistory(**bequest),
        income_beneficiaries=IncomeBeneficiariesHistory(**section(IncomeBeneficiaries)),
        restrictions=RestrictionsContingenciesHistory(**restrictions),
        defined_terms=[
            DefinedTermHistory(term=f"Term {i + 1}", history=hist(f"definition {i + 1}"))
            for i in range(n_terms)
        ],
        um_mentions=[hist(f"mention text {i + 1}") for i in range(n_mentions)],
        found_but_uncategorized=[
            UncategorizedFinding(text=f"uncategorized {i + 1}", citation="p. 1", note="why it matters")
            for i in range(n_uncat)
        ],
        judgment_calls=[f"judgment call {i + 1}" for i in range(n_judgment)],
        questions_for_client=[f"question {i + 1}?" for i in range(n_questions)],
    )


def tiny_single_annotation():
    f = lambda: SingleFinding(status="Found", answer="x", citation="p. 1")
    return DocumentAnnotation(
        document_name="stub",
        bequest_language=BequestLanguage(**{n: f() for n in BequestLanguage.model_fields}),
        income_beneficiaries=IncomeBeneficiaries(**{n: f() for n in IncomeBeneficiaries.model_fields}),
        restrictions=RestrictionsContingencies(**{n: f() for n in RestrictionsContingencies.model_fields}),
    )
