"""
Canned model outputs for the API-free tests: a fictional packet annotation and a minimal
single-document annotation. Pure builders; nothing here touches disk or the network.

Importing this module also makes the tests safe to run: .env is never read, and any model
call that a test forgot to stub raises RealModelCallInTests instead of spending money.
"""

import os

os.environ["CANETRUST_DISABLE_DOTENV"] = "1"

import anthropic


class RealModelCallInTests(AssertionError):
    pass


class _BlockedAnthropic:
    def __init__(self, *args, **kwargs):
        raise RealModelCallInTests(
            "A real Anthropic client was created during tests or the stub server. "
            "Stub service.analyze_document / service.analyze_packet instead.")


anthropic.Anthropic = _BlockedAnthropic

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


# Strict tool use limits (see schema_multi.py): at most 24 optional parameters and 16
# union-typed parameters across the schema; every object closed and fully required; and no
# constraint keywords the API's strict mode does not support.
STRICT_MAX_OPTIONAL = 24
STRICT_MAX_UNION = 16
STRICT_UNSUPPORTED = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                      "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "pattern")


def strict_schema_problems(schema: dict) -> list[str]:
    """Why a JSON schema would fail (or strain) strict tool use; [] when it complies."""
    problems: list[str] = []
    defs = schema.get("$defs", {})
    counts = {"optional": 0, "union": 0}
    seen: set[str] = set()

    def walk(node, path, stack):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in stack:
                problems.append(f"{path}: recursive reference to {name}")
            elif name not in seen:                  # each definition counts once
                seen.add(name)
                walk(defs.get(name, {}), f"{path}->{name}", stack | {name})
            return
        for key in STRICT_UNSUPPORTED:
            if key in node:
                problems.append(f"{path}: unsupported keyword {key}")
        if "anyOf" in node or "oneOf" in node or isinstance(node.get("type"), list):
            counts["union"] += 1
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append(f"{path}: additionalProperties is not false")
            props = node.get("properties", {})
            required = set(node.get("required", []))
            counts["optional"] += len(set(props) - required)
            for name, sub in props.items():
                walk(sub, f"{path}.{name}", stack)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            sub = node.get(key)
            for i, item in enumerate(sub if isinstance(sub, list) else [sub] if sub else []):
                walk(item, f"{path}.{key}[{i}]" if isinstance(sub, list) else f"{path}.{key}", stack)

    walk(schema, "$", frozenset())
    if counts["optional"] > STRICT_MAX_OPTIONAL:
        problems.append(f"{counts['optional']} optional parameters (limit {STRICT_MAX_OPTIONAL})")
    if counts["union"] > STRICT_MAX_UNION:
        problems.append(f"{counts['union']} union-typed parameters (limit {STRICT_MAX_UNION})")
    return problems

