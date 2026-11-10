"""
Chains (docs/DESIGN.md, "Chains"): which instruments amend which, in what order. Pure logic, no
model call.

    build_chains(documents, families=None) -> ChainResult

Input is one ChainDoc per document (from_triaged() converts a triage document; the confirm
screen's edits arrive as the same fields). The rules:

  * Only documents with treatment "annotate" that are not excluded are chained, and only types
    listed in assets/chain_families.json (trust, restated_trust, amendment; will, codicil).
    Annotated documents that cannot be chained (other_dispositive, a missing date) are returned
    in `standalone` and annotated on their own.
  * Documents are grouped by family and normalized principal (case, punctuation, word order,
    Jr./Sr.). A user override `amends` puts a document in its target's chain, as an amendment.
  * Order is the document's own date, never file order or name. A document without a date is
    flagged and left unchained, never guessed. Documents with the same date keep file order and
    are flagged.
  * The latest restatement (or, without one, the latest original) is the base. Every instrument
    before it is superseded: kept, with a reason, not a gap. Instruments after it amend it.
  * A document that revokes another marks it revoked. If a revoked document was a restatement,
    the instruments it had superseded become active again, flagged revival_assumed: an
    assumption for a person to confirm, not a ruling.
  * A referenced amendment or codicil that was not provided is a gap only if its date is known,
    is after the base restatement (or there is no restatement), and no provided document of that
    family has that date. A reference without a usable date is flagged, never reported as a gap.
  * A chain with no base is still built, and flagged.

Flags carry document ids, counts and our own codes only. Gaps and superseded references keep the
reference's description and date for the workbook, never for a log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from settings import ASSETS
from triage import Flag, normalize_principal

FAMILIES_PATH = ASSETS / "chain_families.json"

CHAIN_FLAG_CODES = (
    "date_missing", "same_date", "no_base", "multiple_bases", "revival_assumed", "revokes_unmatched",
    "reference_undated", "chain_gap", "principal_unknown", "override_target_unchained",
    "order_assumed", "not_chainable",
)


@dataclass(frozen=True)
class ChainDoc:
    id: str
    doc_type: str
    principal: str
    date: date | None
    treatment: str
    order: int                                   # position in file order; ties only
    refers_to: tuple[dict, ...] = ()             # {"what": str, "date": "YYYY-MM-DD" | None}
    revokes: tuple[dict, ...] = ()
    excluded: bool = False                       # confirm screen: leave out entirely
    amends: str | None = None                    # confirm screen: this document amends that id
    superseded: bool = False                     # confirm screen: mark superseded


@dataclass
class ChainMember:
    id: str
    role: str                  # base | restatement | amendment
    status: str                # base | active | superseded | revoked
    reason: str = ""           # our own words, ids only
    revival_assumed: bool = False


@dataclass
class Chain:
    id: str
    family: str                                   # trust | will
    base: str | None
    members: list[ChainMember]                    # in date order
    gaps: list[dict] = field(default_factory=list)                      # {"what", "date"}
    superseded_not_provided: list[dict] = field(default_factory=list)   # {"what", "date"}

    @property
    def amendments(self) -> list[str]:
        """Active amendments after the base, in date order."""
        return [m.id for m in self.members if m.status == "active"]

    @property
    def instruments(self) -> list[str]:
        """Base, then every amendment after it (active or revoked): what annotation reads."""
        return [m.id for m in self.members if m.status in ("base", "active", "revoked")]

    def to_dict(self) -> dict:
        return {"id": self.id, "family": self.family, "base": self.base,
                "amendments": self.amendments,
                "members": [vars(m).copy() for m in self.members],
                "gaps": list(self.gaps), "superseded_not_provided": list(self.superseded_not_provided)}


@dataclass
class ChainResult:
    chains: list[Chain]
    standalone: list[str]                         # annotated documents not in any chain
    flags: list[Flag]

    def to_dict(self) -> dict:
        return {"chains": [c.to_dict() for c in self.chains], "standalone": list(self.standalone),
                "flags": [vars(f).copy() for f in self.flags]}


def load_families(path: Path = FAMILIES_PATH) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {name: spec for name, spec in raw.items() if not name.startswith("_")}


def from_triaged(d, *, order: int, excluded: bool = False, amends: str | None = None,
                 superseded: bool = False) -> ChainDoc:
    """A triage.TriagedDocument (or anything with the same attributes) as a ChainDoc."""
    return ChainDoc(
        id=d.id, doc_type=d.doc_type, principal=d.principal, date=d.date, treatment=d.treatment,
        order=order, refers_to=tuple(d.refers_to), revokes=tuple(getattr(d, "revokes", []) or []),
        excluded=excluded, amends=amends, superseded=superseded)


def _ref_date(ref: dict) -> date | None:
    value = ref.get("date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _family_of(doc_type: str, families: dict) -> tuple[str, str] | None:
    for name, spec in families.items():
        if doc_type in spec["roles"]:
            return name, spec["roles"][doc_type]
    return None


def build_chains(documents: list[ChainDoc], families: dict | None = None) -> ChainResult:
    families = families or load_families()
    flags: list[Flag] = []
    standalone: list[str] = []
    by_id = {d.id: d for d in documents}

    # 1. Which documents can be chained, and in which group.
    def group_key(d: ChainDoc):
        fam = _family_of(d.doc_type, families)
        return (fam[0], normalize_principal(d.principal)) if fam else None

    eligible = [d for d in documents if d.treatment == "annotate" and not d.excluded]
    groups: dict[tuple, list[ChainDoc]] = {}
    roles: dict[str, str] = {}
    for d in sorted(eligible, key=lambda x: x.order):
        key, role = None, None
        if d.amends is not None:
            target = by_id.get(d.amends)
            if target is not None and target in eligible and target.amends is None and group_key(target):
                key, role = group_key(target), "amendment"
            else:
                flags.append(Flag("override_target_unchained", d.id, "the document it amends is not chained"))
        if key is None:
            key = group_key(d)
            fam = _family_of(d.doc_type, families)
            role = fam[1] if fam else None
        if key is None:
            standalone.append(d.id)
            flags.append(Flag("not_chainable", d.id, f"type {d.doc_type} is annotated on its own"))
            continue
        if d.date is None and d.amends is None:
            standalone.append(d.id)
            flags.append(Flag("date_missing", d.id, "no date, so it is not placed in a chain"))
            continue
        roles[d.id] = role
        groups.setdefault(key, []).append(d)

    # 2. One chain per group, in file order of each group's first document.
    chains: list[Chain] = []
    ordered_groups = sorted(groups.items(), key=lambda kv: min(d.order for d in kv[1]))
    for n, ((family, principal), docs) in enumerate(ordered_groups, start=1):
        chain_id = f"c{n}"
        if principal is None:
            flags.append(Flag("principal_unknown", None, f"{chain_id}: {len(docs)} document(s) with no principal"))
        chains.append(_build_one(chain_id, family, docs, roles, documents, families, flags))
    return ChainResult(chains, standalone, flags)


def _build_one(chain_id, family, docs, roles, all_documents, families, flags) -> Chain:
    dated = sorted((d for d in docs if d.date is not None), key=lambda d: (d.date, d.order))
    undated = sorted((d for d in docs if d.date is None), key=lambda d: d.order)   # overrides only
    ordered = dated + undated
    for d in undated:
        flags.append(Flag("order_assumed", d.id, f"{chain_id}: no date; placed last by override"))
    for before, after in zip(dated, dated[1:]):
        if before.date == after.date:
            flags.append(Flag("same_date", after.id, f"{chain_id}: same date as {before.id}; file order kept"))

    # Revocations within this chain: match by date.
    revoked_by: dict[str, str] = {}
    for d in ordered:
        for ref in d.revokes:
            when = _ref_date(ref)
            targets = [x for x in dated if when is not None and x.date == when and x.id != d.id]
            if not targets:
                flags.append(Flag("revokes_unmatched", d.id, f"{chain_id}: a revoked instrument is not in this chain"))
                continue
            for t in targets:
                revoked_by.setdefault(t.id, d.id)

    def can_be_base(d):
        return roles[d.id] in ("base", "restatement") and d.id not in revoked_by and not d.superseded

    bases = [d for d in ordered if roles[d.id] == "base" and not d.superseded]
    if len(bases) > 1:
        flags.append(Flag("multiple_bases", bases[-1].id,
                          f"{chain_id}: {len(bases)} original instruments; the latest is used"))
    candidates = [d for d in ordered if can_be_base(d)]
    base = candidates[-1] if candidates else None
    base_index = ordered.index(base) if base else -1

    # A revoked restatement after the base: what it had superseded is active again, by assumption.
    revoked_restatements = [d for d in ordered[base_index + 1:]
                            if roles[d.id] == "restatement" and d.id in revoked_by]
    revival = bool(revoked_restatements)
    last_revoked = ordered.index(revoked_restatements[-1]) if revival else -1

    members: list[ChainMember] = []
    for i, d in enumerate(ordered):
        role = roles[d.id]
        if d.id in revoked_by:
            m = ChainMember(d.id, role, "revoked", f"revoked by {revoked_by[d.id]}")
        elif d.superseded:
            m = ChainMember(d.id, role, "superseded", "marked superseded on the confirm screen")
        elif base is not None and i < base_index:
            m = ChainMember(d.id, role, "superseded", f"superseded by {base.id}")
        elif d is base:
            m = ChainMember(d.id, role, "base")
        else:
            m = ChainMember(d.id, role, "active")
        if revival and m.status in ("base", "active") and i < last_revoked:
            m.revival_assumed = True
            flags.append(Flag("revival_assumed", d.id,
                              f"{chain_id}: active again because {ordered[last_revoked].id} was revoked"))
        members.append(m)
    if base is None:
        flags.append(Flag("no_base", None, f"{chain_id}: no original or restatement in this chain"))

    gaps, superseded_refs = _references(chain_id, family, ordered, members, base, roles,
                                        all_documents, families, flags)
    return Chain(chain_id, family, base.id if base else None, members, gaps, superseded_refs)


def _references(chain_id, family, ordered, members, base, roles, all_documents, families, flags):
    """Gaps (referenced, later than the base restatement, not provided) and references the base
    restatement superseded."""
    spec = families[family]
    words = re.compile(r"\b(?:" + "|".join(map(re.escape, spec["reference_words"])) + r")s?\b", re.I)
    provided = {d.date for d in all_documents if d.date is not None and d.doc_type in spec["roles"]}
    restatement_date = base.date if base is not None and roles[base.id] == "restatement" else None
    status = {m.id: m.status for m in members}
    gaps: dict[date, dict] = {}
    superseded: dict[date, dict] = {}
    undated = 0
    for d in ordered:
        if status[d.id] == "superseded":
            continue
        for ref in d.refers_to:
            when = _ref_date(ref)
            what = str(ref.get("what") or "")
            if when is None:
                undated += bool(words.search(what))
                continue
            if when in provided:
                continue
            if restatement_date is not None and when < restatement_date:
                superseded.setdefault(when, {"what": what, "date": when.isoformat()})
            elif words.search(what) and (restatement_date is None or when > restatement_date):
                gaps.setdefault(when, {"what": what, "date": when.isoformat()})
    if undated:
        flags.append(Flag("reference_undated", None,
                          f"{chain_id}: {undated} referenced instrument(s) without a usable date; not counted as gaps"))
    if gaps:
        flags.append(Flag("chain_gap", base.id if base else None,
                          f"{chain_id}: {len(gaps)} referenced instrument(s) not provided"))
    return ([gaps[k] for k in sorted(gaps)], [superseded[k] for k in sorted(superseded)])
