"""
The pipeline, in memory from end to end. Every entry point calls it:

    cli.py        file paths from config.py  -> load_files -> triage_packet -> annotate_plan
    api/index.py  uploads over HTTP           -> /api/triage: load_files -> triage_packet
                                                 /api/annotate: validate the plan -> annotate_plan
                  the legacy one-file request -> annotate_one_shot (triage, auto-confirm, annotate)

Nothing here reads or writes the working directory. Uploads arrive as bytes and the workbook
leaves as bytes, which is what a serverless function needs.

Stages:
    load_files      fast, no network: page text and size limits. Its errors are the user's
                    to fix (bad file, a scan, too much text).
    triage_packet   one model call (or one per window): the document map, as a Plan.
    annotate_plan   labels and chains recomputed from the plan, then every chain annotated in
                    parallel with the facts lane beside them, then one workbook. One chain
                    failing does not sink the others: the workbook gets the rest and the packet
                    map says what failed.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import anthropic
from pydantic import ValidationError

import annotate as annotate_module
import facts as facts_module
import triage as triage_module
from chains import Chain, ChainMember
from page_reader import ExtractionError, describe_unreadable, unreadable_sheets
from page_source import load_pages
from plan import Plan, PlanFile, chain_plan, label_plan, plan_from_triage
from settings import Settings, require_api_key
from workbook import FAMILY_TITLE, build_workbook, sheet_names

Progress = Callable[[str], None]
MAX_NOTES = 200


def _quiet(_message: str) -> None:
    pass


def model_client(settings: Settings):
    """The one place a real client is made. Tests and the stub server replace this."""
    return anthropic.Anthropic(api_key=require_api_key(settings))


@dataclass
class UploadedFile:
    filename: str
    data: bytes


@dataclass
class LoadedFiles:
    files: list[PlanFile]
    pages_by_file: dict[str, list]
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(p.text) for pages in self.pages_by_file.values() for p in pages)


@dataclass
class TriageOutcome:
    plan: Plan
    loaded: LoadedFiles
    warnings: list[str]
    usage: dict


@dataclass
class AnnotationResult:
    xlsx: bytes
    filename: str
    warnings: list[str] = field(default_factory=list)
    chains: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    route: str = "unified"


class NeedsConfirmation(RuntimeError):
    """Triage needs a person's decision; the one-shot path cannot continue."""


# ---------------------------------------------------------------------------
# Errors, described safely
# ---------------------------------------------------------------------------
def describe_failure(exc: Exception) -> tuple[str, str]:
    """(code, message) safe to show a user. Never includes document text or secrets."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "model_auth", "The server could not authenticate with the model provider. Tell the administrator."
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", "The model provider is rate-limiting requests. Wait a minute and try again."
    if isinstance(exc, anthropic.APIConnectionError):          # includes timeouts
        return "model_unreachable", "Could not reach the model provider. Try again."
    if isinstance(exc, anthropic.APIStatusError):
        return "model_error", f"The model provider returned an error (HTTP {exc.status_code}). Try again."
    if isinstance(exc, ValidationError):
        return "bad_model_output", ("The model's answer did not match the expected form. "
                                    "Try again; if it keeps happening, tell the administrator.")
    if isinstance(exc, NeedsConfirmation):
        return "needs_confirmation", str(exc)
    if isinstance(exc, RuntimeError):                          # our own messages (annotate, settings, triage)
        return "run_failed", str(exc)
    return "internal", "Something went wrong while building the workbook."


# ---------------------------------------------------------------------------
# Files -> pages
# ---------------------------------------------------------------------------
def load_files(files: list[UploadedFile], settings: Settings) -> LoadedFiles:
    """Page records for every file, in upload order. Raises ExtractionError (a file the user must
    fix) or ValueError (too much text). File ids are the file names, made unique."""
    if not files:
        raise ValueError("Upload at least one file.")
    infos, pages_by_file, warnings, taken = [], {}, [], set()
    for upload in files:
        name = upload.filename or "upload"
        file_id, k = name, 1
        while file_id.lower() in taken:
            k += 1
            stem, dot, ext = name.rpartition(".")
            file_id = f"{stem} ({k}).{ext}" if dot else f"{name} ({k})"
        taken.add(file_id.lower())
        pages = load_pages(file_id, name, upload.data, max_pages=settings.max_pages)
        pages_by_file[file_id] = pages
        infos.append(PlanFile(file_id, name, len(pages)))
        warning = describe_unreadable(file_id, unreadable_sheets(pages))
        if warning:
            warnings.append(warning)
    loaded = LoadedFiles(infos, pages_by_file, warnings)
    limit = settings.max_chars * 2
    if loaded.total_chars > limit:
        raise ValueError(f"The files total {loaded.total_chars:,} characters, over the {limit:,}-character "
                         f"limit for one packet.")
    return loaded


def matter_name(files: list[PlanFile]) -> str:
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", files[0].filename) if files else "annotation"


def output_filename(name: str) -> str:
    base = re.sub(r"[^\w\-. ()]+", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    return f"{(base or 'annotation')[:80]}_annotation.xlsx"


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------
def triage_packet(loaded: LoadedFiles, settings: Settings, progress: Progress = _quiet,
                  name: str | None = None) -> TriageOutcome:
    sheets = sum(len(p) for p in loaded.pages_by_file.values())
    progress(f"Sorting {sheets} sheet(s) in {len(loaded.files)} file(s) into documents.")
    result = triage_module.triage(loaded.pages_by_file, settings, client=model_client(settings))
    plan = plan_from_triage(name or matter_name(loaded.files), loaded.files, result)
    progress(f"Found {len(plan.documents)} document(s).")
    return TriageOutcome(plan, loaded, loaded.warnings + result.warnings, dict(result.usage))


def auto_confirm_problem(plan: Plan) -> str | None:
    """Why the one-shot path must not proceed without a person, or None."""
    if plan.needs_attention:
        return ("Triage could not settle where every document starts and ends. Use the confirm "
                "screen (POST /api/triage, then /api/annotate) to check the document map.")
    if any(f.get("code") == "possible_multiple_matters" for f in plan.flags):
        return ("These files may belong to more than one matter. Use the confirm screen to choose "
                "what to annotate.")
    return None


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------
def _standalone_chain(index: int, doc) -> Chain:
    family = ("will" if doc.doc_type in ("will", "codicil")
              else "trust" if doc.doc_type in ("trust", "restated_trust", "amendment") else "other")
    return Chain(f"s{index}", family, doc.id, [ChainMember(doc.id, "base", "base")])


def _chain_notes(chain: Chain, ci, plan: Plan, outcome) -> list[str]:
    notes = []
    for m in chain.members:
        d = plan.document(m.id)
        if m.status == "superseded":
            notes.append(f"Superseded, not annotated: {d.title} ({d.date or 'undated'}, {m.id}): {m.reason}.")
        if m.revival_assumed:
            notes.append(f"Revival assumed: {d.title} ({m.id}) is treated as in force because a later "
                         f"restatement was revoked. Confirm this.")
    for r in chain.superseded_not_provided:
        notes.append(f"Superseded by the base, not provided (not a gap): {r['what']} ({r['date']}).")
    for g in chain.gaps:
        notes.append(f"NOT PROVIDED (gap in the chain): {g['what']} ({g['date']}).")
    if not ci.has_base:
        notes.append("No base instrument (original, restatement or will) was provided for this chain.")
    if chain.id.startswith("s"):
        notes.append("Annotated on its own: this document could not be placed in a chain (see the packet map).")
    for path, tag, label in (outcome.citation_problems if outcome else []):
        notes.append(f"Cited page not found: {path} cites p. {label}, which is not a page of {tag}.")
    return notes


def _chain_text(chain: Chain, plan: Plan) -> dict[str, str]:
    """{doc id: 'c1 base' | 'c1 Amendment 2' | 'c1 superseded (by d3)' ...} for the packet map."""
    out = {}
    number = 0
    for m in chain.members:
        if m.status == "superseded":
            out[m.id] = f"{chain.id} superseded ({m.reason})"
        elif m.id == chain.base:
            out[m.id] = f"{chain.id} base"
        else:
            number += 1
            out[m.id] = f"{chain.id} {annotate_module.AMENDMENT_WORD.get(chain.family, 'Amendment')} {number}" + (
                f" ({m.reason})" if m.status == "revoked" else "") + (" (revival assumed)" if m.revival_assumed else "")
    return out


def annotate_plan(plan: Plan, pages_by_file: dict[str, list], settings: Settings,
                  progress: Progress = _quiet) -> AnnotationResult:
    """Label, chain, annotate every chain in parallel beside the facts lane, build the workbook."""
    require_api_key(settings)
    warnings = label_plan(plan, pages_by_file)
    chained = chain_plan(plan)
    chains = list(chained.chains)
    by_id = {d.id: d for d in plan.documents}
    for k, doc_id in enumerate(chained.standalone, start=1):
        chains.append(_standalone_chain(k, by_id[doc_id]))
    names = sheet_names([
        (by_id[c.base].title if c.base and by_id[c.base].title.lower() != "unknown" else "")
        or f"{FAMILY_TITLE.get(c.family, 'Instrument')} chain {c.id}" for c in chains])
    inputs = [annotate_module.chain_input(c, by_id, name) for c, name in zip(chains, names)]
    progress(f"{len(chains)} chain(s) to annotate"
             + (f", {len(chained.standalone)} document(s) on their own." if chained.standalone else "."))

    with ThreadPoolExecutor(max_workers=1) as side:
        facts_future = side.submit(_run_facts, plan, settings, progress)
        outcomes = annotate_module.annotate_chains(inputs, settings, progress,
                                                   make_client=lambda: model_client(settings))
        facts_result, facts_error = facts_future.result()

    if outcomes and all(o.error is not None for o in outcomes):
        raise outcomes[0].error

    contacts = facts_module.contact_rows(facts_result) if facts_result else [
        {"label": "Contacts", "answer": None, "citation": None, "status": "Could not determine",
         "note": f"The facts lane failed: {describe_failure(facts_error)[1]}" if facts_error else None}]

    sheets, chain_rows, chain_of = [], [], {}
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    for chain, ci, name, outcome in zip(chains, inputs, names, outcomes):
        chain_of.update(_chain_text(chain, plan))
        for key in usage:
            usage[key] += outcome.usage.get(key, 0)
        warnings += outcome.warnings
        base = f"{chain.base} ({by_id[chain.base].title})" if chain.base else "none"
        row = {"id": chain.id, "family": chain.family, "base": base,
               "amendments": ", ".join(i.doc_id for i in ci.amendments) or "none"}
        if outcome.error is not None:
            code, message = describe_failure(outcome.error)
            chain_rows.append({**row, "sheet": "(none)", "result": f"FAILED ({code}): {message}"})
            warnings.append(f"{chain.id}: could not be annotated ({code})")
            continue
        sheets.append({"name": name, "annotation": outcome.annotation, "input": ci,
                       "notes": _chain_notes(chain, ci, plan, outcome)})
        problems = len(outcome.citation_problems)
        chain_rows.append({**row, "sheet": name, "result": "annotated" + (
            f"; {problems} cited page label(s) not found" if problems else "")})
    if facts_result:
        usage = {k: usage[k] + facts_result.usage.get(k, 0) for k in usage}
        warnings += facts_result.warnings

    chain_flags: dict[str | None, list[str]] = {}
    for f in chained.flags:
        chain_flags.setdefault(f.document, []).append(f"{f.code}: {f.detail}")
    packet_rows = []
    files = {f.file_id: f for f in plan.files}
    for d in plan.documents:
        flags = [f"{f['code']}: {f['detail']}" for f in d.flags] + chain_flags.get(d.id, [])
        packet_rows.append({
            "file": files[d.file_id].filename,
            "sheets": f"{d.first_sheet}" if d.first_sheet == d.last_sheet else f"{d.first_sheet}-{d.last_sheet}",
            "id": d.id, "type": d.doc_type, "title": d.title,
            "date": d.date.isoformat() if d.date else None,
            "treatment": "excluded" if d.excluded else d.treatment,
            "reason": "; ".join(x for x in (d.treatment_reason, d.note) if x),
            "chain": chain_of.get(d.id, ""), "flags": "\n".join(flags)})
    notes = ([f"{f['code']}: {f['detail']}" for f in plan.flags]
             + [f"{c}" for c in chain_flags.get(None, [])] + warnings)
    if len(notes) > MAX_NOTES:
        notes = notes[:MAX_NOTES] + [f"... and {len(notes) - MAX_NOTES} more"]

    progress("Filling in the workbook.")
    xlsx = build_workbook(settings.template, sheets, packet_rows, chain_rows, notes, contacts)
    return AnnotationResult(xlsx, output_filename(plan.name), warnings, chain_rows, usage)


def _run_facts(plan: Plan, settings: Settings, progress: Progress):
    try:
        result = facts_module.extract_facts(plan.documents, settings, client=model_client(settings))
    except Exception as exc:                            # facts are useful, never essential
        progress("Contacts could not be read.")
        return None, exc
    progress("Contacts and court facts read.")
    return result, None


# ---------------------------------------------------------------------------
# One shot: the legacy request and the CLI
# ---------------------------------------------------------------------------
def annotate_one_shot(files: list[UploadedFile], settings: Settings, progress: Progress = _quiet,
                      name: str | None = None) -> AnnotationResult:
    """Triage, accept triage's plan as it stands, annotate. Refuses (NeedsConfirmation) when
    triage needs a person."""
    require_api_key(settings)
    loaded = load_files(files, settings)
    outcome = triage_packet(loaded, settings, progress, name=name)
    problem = auto_confirm_problem(outcome.plan)
    if problem:
        raise NeedsConfirmation(problem)
    result = annotate_plan(outcome.plan, loaded.pages_by_file, settings, progress)
    result.warnings = outcome.warnings + result.warnings
    result.usage = {k: result.usage.get(k, 0) + outcome.usage.get(k, 0) for k in result.usage}
    return result
