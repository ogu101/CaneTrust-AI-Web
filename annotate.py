"""
Annotation: one model call per chain (docs/DESIGN.md, "Chains" and "Workbook").

    chain_input(chain, documents, name)     -> ChainInput      what the model reads
    annotate_chain(ci, settings, client)    -> ChainOutcome    one call, validated
    annotate_chains(inputs, settings, ...)  -> [ChainOutcome]  in parallel; one failure sinks nothing

Prompt layout:
  system[0]  assets/annotation_rules.md, cached: identical on every call.
  tools[0]   the ChainAnnotation schema, cached: field-level instructions travel with the field.
  user       the chain: its instruments in order, with their tags and statuses, what the base
             superseded, what is missing, then every instrument's text with [Page <label>]
             markers from the page labeler (printed numbers, never PDF positions).

The tool is sent WITHOUT strict (the API rejects this schema's grammar as too large; see
annotation_schema.py), so the answer is validated here: pydantic, then reconcile() lines the
per-amendment entries up with the chain, then check_citations() confirms every cited page label
exists in the instrument it names. A label that does not is flagged and noted in that cell.

Nothing here logs or raises with document text or model output. Errors carry chain ids, counts
and our own words; raw output is written only to CANETRUST_DEBUG_DIR.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from annotation_schema import ChainAnnotation, Finding, iter_histories, reconcile
from settings import Settings, require_api_key
from triage import NO_FORCED_TOOL_CHOICE

TOOL_NAME = "analyze_documents"
STRICT = False                 # docs/PROGRESS.md D0.1: the API rejects this schema in strict mode
MAX_AMENDMENTS = 8             # amendment columns in the workbook template

BASE_TAG = {"trust": "Trust", "will": "Will", "other": "Instrument"}
AMENDMENT_WORD = {"trust": "Amendment", "will": "Codicil", "other": "Amendment"}


class AnnotationError(RuntimeError):
    """A chain could not be annotated. The message is ours: ids, counts, no document text."""


@dataclass
class Instrument:
    tag: str                          # "Trust", "Will", "Instrument", "Amendment 2", "Codicil 1"
    number: int                       # 0 = base; 1.. = amendments in date order
    doc_id: str
    title: str
    doc_type: str
    date: date | None
    status: str                       # BASE | ACTIVE | REVOKED by <tag>
    revival_assumed: bool
    pages: list                       # labelled PageRecords

    @property
    def labels(self) -> set[str]:
        return {(p.label or "").lower() for p in self.pages}


@dataclass
class ChainInput:
    chain_id: str
    family: str                       # trust | will | other
    name: str                         # the chain's display name (its sheet name)
    instruments: list[Instrument]     # base first when there is one, then amendments
    has_base: bool
    superseded: list[str] = field(default_factory=list)          # descriptions (document text)
    missing: list[str] = field(default_factory=list)             # gaps (document text)

    @property
    def amendments(self) -> list[Instrument]:
        return [i for i in self.instruments if i.number > 0]

    @property
    def amendment_numbers(self) -> list[int]:
        return [i.number for i in self.amendments]

    @property
    def total_chars(self) -> int:
        return sum(len(p.text) for i in self.instruments for p in i.pages)


@dataclass
class ChainOutcome:
    chain_id: str
    annotation: ChainAnnotation | None = None
    warnings: list[str] = field(default_factory=list)
    citation_problems: list[tuple[str, str, str]] = field(default_factory=list)   # (path, tag, label)
    usage: dict = field(default_factory=dict)
    error: Exception | None = None


# ---------------------------------------------------------------------------
# What the model reads
# ---------------------------------------------------------------------------
def _when(d: date | None) -> str:
    return d.isoformat() if d else "undated"


def chain_input(chain, documents: dict, name: str) -> ChainInput:
    """chain: a chains.Chain; documents: {id: object with title, doc_type, date, pages}."""
    family = chain.family
    members = {m.id: m for m in chain.members}
    ordered = chain.instruments                      # base, then amendments (active or revoked)
    tags: dict[str, str] = {}
    number = 0
    for doc_id in ordered:
        if doc_id == chain.base:
            tags[doc_id] = BASE_TAG.get(family, "Instrument")
        else:
            number += 1
            tags[doc_id] = f"{AMENDMENT_WORD.get(family, 'Amendment')} {number}"
    instruments, number = [], 0
    for doc_id in ordered:
        d, m = documents[doc_id], members[doc_id]
        if doc_id == chain.base:
            n, status = 0, "BASE"
        else:
            number += 1
            n = number
            revoker = m.reason.removeprefix("revoked by ") if m.status == "revoked" else None
            status = f"REVOKED by {tags.get(revoker, 'a later instrument')}" if revoker else "ACTIVE"
        instruments.append(Instrument(tags[doc_id], n, doc_id, d.title, d.doc_type, d.date, status,
                                      m.revival_assumed, list(d.pages)))
    superseded = [f"{documents[m.id].title}, {_when(documents[m.id].date)} ({m.id})"
                  for m in chain.members if m.status == "superseded"]
    superseded += [f"{r['what']}, {r['date']} (referenced, not provided)" for r in chain.superseded_not_provided]
    missing = [f"{g['what']}, {g['date']}" for g in chain.gaps]
    return ChainInput(chain.id, family, name, instruments, chain.base is not None, superseded, missing)


def render_chain(ci: ChainInput) -> str:
    lines = [f"Chain: {ci.name}", f"Document family: {ci.family}", "",
             "Instruments in this chain, in date order:"]
    if not ci.has_base:
        lines.append("  - BASE INSTRUMENT: NOT PROVIDED (no original, restatement or will in this chain)")
    for i in ci.instruments:
        status = i.status + ("; revival assumed" if i.revival_assumed else "")
        lines.append(f"  - {i.tag} [{status}]: {i.title}; {i.doc_type}; dated {_when(i.date)}")
    if ci.superseded:
        lines += ["", "Superseded by the base (not sent; replaced, not missing):"]
        lines += [f"  - {s}" for s in ci.superseded]
    if ci.missing:
        lines += ["", "Referenced but NOT PROVIDED (gaps in the chain):"]
        lines += [f"  - {s}" for s in ci.missing]
    lines.append("")
    if ci.amendment_numbers:
        lines.append(f"Amendment numbers to report on, exactly: {ci.amendment_numbers}")
    else:
        lines.append("This chain has no amendments: `instruments` and every `amendments` list are "
                     "empty, and every `operative` is null.")
    for i in ci.instruments:
        lines += ["", f"=== {i.tag} [{i.status}] — {i.title} ==="]
        for p in i.pages:
            if len(p.text.strip()) < 40:
                lines.append(f"[Page {p.label}: no extractable text]")
            else:
                lines += [f"[Page {p.label}]", p.text]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def tool_definition() -> dict:
    tool = {
        "name": TOOL_NAME,
        "description": ("Record the complete structured annotation of this chain. Call this exactly "
                        "once, after reading every instrument in order."),
        "input_schema": ChainAnnotation.model_json_schema(),
        "cache_control": {"type": "ephemeral"},
    }
    if STRICT:
        tool["strict"] = True
    return tool


def build_request(ci: ChainInput, settings: Settings) -> dict:
    forced = not settings.model.startswith(NO_FORCED_TOOL_CHOICE)
    rules = Path(settings.annotation_rules).read_text(encoding="utf-8")
    message = render_chain(ci)
    if not forced:
        message += f"\n\nCall {TOOL_NAME} exactly once with your complete findings."
    return dict(
        model=settings.model,
        max_tokens=settings.max_tokens_annotate,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.effort},
        system=[{"type": "text", "text": rules, "cache_control": {"type": "ephemeral"}}],
        tools=[tool_definition()],
        tool_choice={"type": "tool", "name": TOOL_NAME} if forced else {"type": "auto"},
        messages=[{"role": "user", "content": message}],
    )


# ---------------------------------------------------------------------------
# Cited page labels
# ---------------------------------------------------------------------------
_LABEL = r"unnumbered,\s*sheet\s+\d+\s+of\s+\d+|\d{1,4}|[ivxlcdm]{1,8}"
_PAGE_RE = re.compile(rf"\b(?:pp?\.|pages?)\s*(?P<a>{_LABEL})(?:\s*[-–]\s*(?P<b>{_LABEL}))?(?![\w])", re.I)
_TAG_RE = re.compile(r"\b(?:(?P<word>Amendment|Codicil)\s+(?P<n>\d+)|(?P<base>Trust|Will|Instrument))\b", re.I)


def _cited(text: str | None):
    """(tag or None, label) for every page cited in a citation string."""
    for segment in re.split(r";", text or ""):
        tags = list(_TAG_RE.finditer(segment))
        numbered = [t for t in tags if t["n"]]
        tag = (f"#{numbered[0]['n']}" if numbered else "#0" if tags else None)
        for m in _PAGE_RE.finditer(segment):
            for label in (m["a"], m["b"]):
                if label:
                    yield tag, " ".join(label.lower().split())


def check_citations(annotation: ChainAnnotation, ci: ChainInput) -> list[tuple[str, str, str]]:
    """Every cited page label must exist in the instrument it names (or, with no tag, in the
    instrument the cell is about; for operative cells, in any instrument). Each problem is noted
    in that cell for the reviewer. Returns (field path, instrument tag, label)."""
    by_number = {i.number: i for i in ci.instruments}
    every = set().union(*(i.labels for i in ci.instruments)) if ci.instruments else set()
    problems: list[tuple[str, str, str]] = []

    def bad(text, default: int | None) -> list[tuple[str, str]]:
        out = []
        for tag, label in _cited(text):
            number = int(tag[1:]) if tag else default
            if number is None:
                ok, name = label in every, "this chain"
            else:
                inst = by_number.get(number)
                ok, name = (inst is not None and label in inst.labels), (inst.tag if inst else f"#{number}")
            if not ok:
                out.append((name, label))
        return out

    def note(found: Finding, issues):
        extra = " ".join(f"[Check: p. {label} is not a page of {name}.]" for name, label in issues)
        found.note = f"{found.note} {extra}".strip() if found.note else extra

    for path, history in iter_histories(annotation):
        for part, finding, default in (("original", history.original, 0),
                                       ("operative", history.operative, None)):
            if finding is None:
                continue
            issues = bad(finding.citation, default)
            if issues:
                note(finding, issues)
                problems += [(f"{path}.{part}", n, l) for n, l in issues]
        for entry in history.amendments:
            issues = bad(entry.citation, entry.amendment_number)
            if issues:
                entry.description = (entry.description + " " + " ".join(
                    f"[Check: p. {label} is not a page of {name}.]" for name, label in issues)).strip()
                problems += [(f"{path}.amendments[{entry.amendment_number}]", n, l) for n, l in issues]
    for k, item in enumerate(annotation.found_but_uncategorized):
        issues = bad(item.citation, None)
        if issues:
            item.note = ((item.note or "") + " " + " ".join(
                f"[Check: p. {label} is not a page of {name}.]" for name, label in issues)).strip()
            problems += [(f"found_but_uncategorized[{k}]", n, l) for n, l in issues]
    return problems


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
def annotate_chain(ci: ChainInput, settings: Settings, client=None) -> ChainOutcome:
    if len(ci.amendments) > MAX_AMENDMENTS:
        raise AnnotationError(f"chain {ci.chain_id} has {len(ci.amendments)} amendments; the workbook "
                              f"has room for {MAX_AMENDMENTS}")
    if ci.total_chars > settings.max_chars:
        raise AnnotationError(f"chain {ci.chain_id} is {ci.total_chars:,} characters, over the "
                              f"{settings.max_chars:,}-character limit for one chain")
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=require_api_key(settings))
    with client.messages.stream(**build_request(ci, settings)) as stream:
        response = stream.get_final_message()
    u = getattr(response, "usage", None)
    usage = {"calls": 1, **{k: int(getattr(u, k, 0) or 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}}
    blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"
              and getattr(b, "name", None) == TOOL_NAME]
    if not blocks:
        raise AnnotationError(f"no {TOOL_NAME} call was returned for chain {ci.chain_id} "
                              f"(stop_reason={response.stop_reason})")
    raw = blocks[0].input
    if settings.debug_dir is not None:
        settings.debug_dir.mkdir(parents=True, exist_ok=True)
        (settings.debug_dir / f"last_raw_annotation_{ci.chain_id}.json").write_text(json.dumps(raw, indent=2))
    if response.stop_reason == "max_tokens":
        raise AnnotationError(
            f"Output was cut off at max_tokens={settings.max_tokens_annotate} for chain {ci.chain_id}, "
            f"so the result is incomplete. Raise CANETRUST_MAX_TOKENS_ANNOTATE (thinking tokens "
            f"share this budget).")
    annotation = ChainAnnotation.model_validate(raw)
    annotation.document_name = ci.name
    warnings = [f"{ci.chain_id}: {w}" for w in reconcile(annotation, ci.amendment_numbers)]
    problems = check_citations(annotation, ci)
    if problems:
        warnings.append(f"{ci.chain_id}: {len(problems)} cited page label(s) not found in their instrument")
    return ChainOutcome(ci.chain_id, annotation, warnings, problems, usage)


def annotate_chains(inputs: list[ChainInput], settings: Settings, progress: Callable[[str], None],
                    make_client: Callable[[], object] | None = None, max_workers: int = 4) -> list[ChainOutcome]:
    """Every chain in parallel. A chain that fails returns an outcome with `error` set; the rest
    are unaffected. Results come back in input order."""
    def run(ci: ChainInput) -> ChainOutcome:
        n = len(ci.amendments)
        progress(f"Annotating {ci.chain_id} ({ci.family}, {n} amendment{'s' if n != 1 else ''}).")
        try:
            outcome = annotate_chain(ci, settings, make_client() if make_client else None)
        except Exception as exc:                      # reported per chain, never raised
            progress(f"{ci.chain_id} could not be annotated.")
            return ChainOutcome(ci.chain_id, error=exc)
        progress(f"{ci.chain_id} annotated.")
        return outcome

    if not inputs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(inputs)))) as pool:
        return list(pool.map(run, inputs))
