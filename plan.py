"""
The plan: what triage proposed and a person confirmed (docs/DESIGN.md, "Confirm screen").

    plan_from_triage(name, files, result)   triage's map as a Plan
    Plan.to_dict() / validate_plan(raw)      the JSON the browser holds and sends back
    label_plan(plan, pages_by_file)          printed labels per document (recomputed every time)
    chain_plan(plan)                          chains.build_chains over the plan

The server never trusts a plan it gets back: validate_plan() checks every file and sheet is
covered exactly once, ranges, ids, types, treatments, dates and overrides, and refuses the plan
with problems that name ids, files and sheets only. Labels and chains are always recomputed from
the boundaries, never taken from the client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from chains import ChainResult, build_chains, from_triaged
from page_labels import label_document
from triage_schema import DOC_TYPES, TREATMENTS

PLAN_VERSION = 1
MAX_TEXT_FIELD = 300          # title, principal, note: a person's edits, kept short


@dataclass
class PlanFile:
    file_id: str
    filename: str
    sheets: int


@dataclass
class PlanDocument:
    id: str
    file_id: str
    first_sheet: int
    last_sheet: int
    doc_type: str
    title: str = "unknown"
    principal: str = "unknown"
    date: date | None = None
    refers_to: list[dict] = field(default_factory=list)
    revokes: list[dict] = field(default_factory=list)
    appears_to_gift_to_university: str = "unclear"
    treatment: str = "log_only"
    treatment_reason: str = ""
    confidence: str = "medium"
    excluded: bool = False
    amends: str | None = None
    superseded: bool = False
    note: str = ""
    flags: list[dict] = field(default_factory=list)      # triage flags for this document
    pages: list = field(default_factory=list)            # labelled PageRecords; never serialized

    def to_dict(self) -> dict:
        return {
            "id": self.id, "file_id": self.file_id, "first_sheet": self.first_sheet,
            "last_sheet": self.last_sheet, "doc_type": self.doc_type, "title": self.title,
            "principal": self.principal, "date": self.date.isoformat() if self.date else None,
            "refers_to": list(self.refers_to), "revokes": list(self.revokes),
            "appears_to_gift_to_university": self.appears_to_gift_to_university,
            "treatment": self.treatment, "treatment_reason": self.treatment_reason,
            "confidence": self.confidence, "excluded": self.excluded, "amends": self.amends,
            "superseded": self.superseded, "note": self.note, "flags": list(self.flags),
            "labels": [{"sheet": p.position, "label": p.label, "source": p.label_source} for p in self.pages],
        }


@dataclass
class Plan:
    name: str
    files: list[PlanFile]
    documents: list[PlanDocument]
    flags: list[dict] = field(default_factory=list)       # packet-level triage flags
    needs_attention: bool = False

    def to_dict(self) -> dict:
        return {"version": PLAN_VERSION, "name": self.name,
                "files": [vars(f).copy() for f in self.files],
                "documents": [d.to_dict() for d in self.documents],
                "flags": list(self.flags), "needs_attention": self.needs_attention}

    def document(self, doc_id: str) -> PlanDocument:
        return next(d for d in self.documents if d.id == doc_id)


def plan_from_triage(name: str, files: list[PlanFile], result) -> Plan:
    """A triage.TriageResult as a Plan, in file order then sheet order."""
    by_doc: dict[str, list[dict]] = {}
    packet_flags = []
    for f in result.flags:
        entry = {"code": f.code, "document": f.document, "detail": f.detail}
        (by_doc.setdefault(f.document, []) if f.document else packet_flags).append(entry)
    docs = [PlanDocument(
        id=d.id, file_id=d.file_id, first_sheet=d.first_sheet, last_sheet=d.last_sheet,
        doc_type=d.doc_type, title=d.title, principal=d.principal, date=d.date,
        refers_to=list(d.refers_to), revokes=list(getattr(d, "revokes", []) or []),
        appears_to_gift_to_university=d.appears_to_gift_to_university, treatment=d.treatment,
        treatment_reason=d.treatment_reason, confidence=d.confidence,
        flags=by_doc.get(d.id, []), pages=list(d.pages)) for d in result.documents]
    return Plan(name, files, docs, packet_flags, result.needs_attention)


# ---------------------------------------------------------------------------
# Validation of a plan sent back by the browser
# ---------------------------------------------------------------------------
def _parse_day(value) -> tuple[date | None, bool]:
    if value in (None, "", "unknown"):
        return None, True
    if not isinstance(value, str) or len(value) != 10:
        return None, False
    try:
        return date.fromisoformat(value), True
    except ValueError:
        return None, False


def _refs(value, where: str, problems: list[str]) -> list[dict]:
    if not isinstance(value, list):
        problems.append(f"{where}: must be a list")
        return []
    out = []
    for r in value:
        day, ok = _parse_day(r.get("date") if isinstance(r, dict) else None)
        if not isinstance(r, dict) or not ok:
            problems.append(f"{where}: each entry needs a 'what' and a YYYY-MM-DD date or null")
            continue
        out.append({"what": str(r.get("what") or "")[:MAX_TEXT_FIELD], "date": day.isoformat() if day else None})
    return out


def validate_plan(raw, sheets_by_file: dict[str, int]) -> tuple[Plan | None, list[str]]:
    """(plan, problems). Problems name ids, files, sheets and fields; never document text.
    sheets_by_file comes from the server's own reading of the page texts, not from the plan."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["the plan must be a JSON object"]
    if raw.get("version") != PLAN_VERSION:
        problems.append(f"plan version must be {PLAN_VERSION}")
    files_raw = raw.get("files")
    docs_raw = raw.get("documents")
    if not isinstance(files_raw, list) or not isinstance(docs_raw, list) or not docs_raw:
        return None, problems + ["the plan needs a 'files' list and a non-empty 'documents' list"]

    files: list[PlanFile] = []
    for i, f in enumerate(files_raw, start=1):
        if not isinstance(f, dict) or f.get("file_id") not in sheets_by_file:
            problems.append(f"file {i}: file_id is not one of the files whose pages were sent")
            continue
        files.append(PlanFile(f["file_id"], str(f.get("filename") or f["file_id"]), sheets_by_file[f["file_id"]]))
    if {f.file_id for f in files} != set(sheets_by_file):
        problems.append("the plan's files do not match the files whose pages were sent")

    docs: list[PlanDocument] = []
    seen: set[str] = set()
    for i, d in enumerate(docs_raw, start=1):
        where = f"document {i}"
        if not isinstance(d, dict):
            problems.append(f"{where}: must be an object")
            continue
        doc_id = d.get("id")
        if not isinstance(doc_id, str) or not doc_id or len(doc_id) > 40 or doc_id in seen:
            problems.append(f"{where}: id is missing, too long or repeated")
            continue
        seen.add(doc_id)
        where = f"document {doc_id}"
        file_id, first, last = d.get("file_id"), d.get("first_sheet"), d.get("last_sheet")
        if file_id not in sheets_by_file:
            problems.append(f"{where}: file_id is not one of the files sent")
            continue
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (first, last)) \
                or not 1 <= first <= last <= sheets_by_file[file_id]:
            problems.append(f"{where}: sheets must be whole numbers within 1-{sheets_by_file[file_id]}, first <= last")
            continue
        if d.get("doc_type") not in DOC_TYPES:
            problems.append(f"{where}: doc_type is not a known type")
            continue
        if d.get("treatment") not in TREATMENTS:
            problems.append(f"{where}: treatment must be one of {', '.join(TREATMENTS)}")
            continue
        day, ok = _parse_day(d.get("date"))
        if not ok:
            problems.append(f"{where}: date must be YYYY-MM-DD or null")
            continue
        for key in ("excluded", "superseded"):
            if not isinstance(d.get(key, False), bool):
                problems.append(f"{where}: {key} must be true or false")
        amends = d.get("amends")
        if amends is not None and (not isinstance(amends, str) or amends == doc_id):
            problems.append(f"{where}: amends must be another document's id or null")
            amends = None
        docs.append(PlanDocument(
            id=doc_id, file_id=file_id, first_sheet=first, last_sheet=last, doc_type=d["doc_type"],
            title=str(d.get("title") or "unknown")[:MAX_TEXT_FIELD],
            principal=str(d.get("principal") or "unknown")[:MAX_TEXT_FIELD], date=day,
            refers_to=_refs(d.get("refers_to", []), f"{where} refers_to", problems),
            revokes=_refs(d.get("revokes", []), f"{where} revokes", problems),
            appears_to_gift_to_university=d.get("appears_to_gift_to_university") if d.get(
                "appears_to_gift_to_university") in ("yes", "no", "unclear") else "unclear",
            treatment=d["treatment"], treatment_reason=str(d.get("treatment_reason") or "")[:MAX_TEXT_FIELD],
            confidence=d.get("confidence") if d.get("confidence") in ("high", "medium", "low") else "medium",
            excluded=d.get("excluded") is True, amends=amends, superseded=d.get("superseded") is True,
            note=str(d.get("note") or "")[:MAX_TEXT_FIELD],
            flags=[f for f in d.get("flags", []) if isinstance(f, dict)][:50]))

    ids = {d.id for d in docs}
    for d in docs:
        if d.amends is not None and d.amends not in ids:
            problems.append(f"document {d.id}: amends names a document that is not in the plan")
    for f in files:
        counts = [0] * (f.sheets + 1)
        for d in docs:
            if d.file_id == f.file_id:
                for s in range(d.first_sheet, d.last_sheet + 1):
                    counts[s] += 1
        missing = [s for s in range(1, f.sheets + 1) if counts[s] == 0]
        twice = [s for s in range(1, f.sheets + 1) if counts[s] > 1]
        if missing:
            problems.append(f"{f.file_id}: {len(missing)} sheet(s) are in no document (first: sheet {missing[0]})")
        if twice:
            problems.append(f"{f.file_id}: {len(twice)} sheet(s) are in more than one document (first: sheet {twice[0]})")
    if problems:
        return None, problems
    order = {f.file_id: i for i, f in enumerate(files)}
    docs.sort(key=lambda d: (order[d.file_id], d.first_sheet))
    name = str(raw.get("name") or "annotation")[:120]
    flags = [f for f in raw.get("flags", []) if isinstance(f, dict)][:50] if isinstance(raw.get("flags"), list) else []
    return Plan(name, files, docs, flags, bool(raw.get("needs_attention"))), []


# ---------------------------------------------------------------------------
# Labels and chains, always recomputed
# ---------------------------------------------------------------------------
def label_plan(plan: Plan, pages_by_file: dict[str, list]) -> list[str]:
    """Label every document's pages from its own range. Returns warnings (ids, sheets, numbers)."""
    warnings = []
    for d in plan.documents:
        labelled, label_warnings = label_document(pages_by_file[d.file_id][d.first_sheet - 1:d.last_sheet])
        d.pages = labelled
        warnings += [f"{d.id}: {w}" for w in label_warnings]
    return warnings


def chain_plan(plan: Plan) -> ChainResult:
    return build_chains([
        from_triaged(d, order=i, excluded=d.excluded, amends=d.amends, superseded=d.superseded)
        for i, d in enumerate(plan.documents)])
