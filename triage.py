"""
Triage (docs/DESIGN.md, "Triage" and "Default treatments"): pages in, a document map out.

    triage(pages_by_file, settings, client=None) -> TriageResult

One forced call to record_triage over short previews of every page (triage_previews.py). The
model decides boundaries, type, title, principal, date, references, whether a document appears
to gift to the University, and a proposed treatment. Code then:

  1. validates: every sheet of every file covered exactly once, ranges inside one file,
     first <= last, types in the enum, dates parse. On failure it retries ONCE with the problems
     listed by position and count. If the retry fails too it repairs conservatively (unclaimed
     sheets become "unknown" documents flagged unassigned, the earlier claim wins an overlap,
     unparseable dates become None flagged date_unreadable) and sets needs_attention;
  2. applies the default-treatments table, which is authoritative (a blank form is log_only);
  3. labels every document's pages (page_labels.label_document);
  4. adds cross-check flags. They never change the map.

Packets longer than the window setting are split into overlapping windows, one call each, and
documents cut by a window edge are stitched back together.

Never log, print, or put into a warning, flag or error any page text or any text the model
returned (titles, names, reasons, evidence). Messages carry ids, sheet numbers, counts, field
names and our own enum values only. Raw model output is written only to CANETRUST_DEBUG_DIR.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path

from page_labels import label_document
from page_reader import PageRecord
from settings import Settings, require_api_key
from triage_previews import Preview, build_previews, estimate_tokens
from triage_schema import (
    DOC_TYPES, TOOL_NAME, TREATMENTS, TriageDocument, clean_evidence, parse_date, parse_documents,
    tool_definition,
)

# Models that reject a forced tool_choice ({"type": "tool"}) with a 400. They get "auto" plus an
# instruction, and a missing tool call counts as a validation failure.
NO_FORCED_TOOL_CHOICE = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")

FLAG_CODES = (
    "unassigned", "date_unreadable", "treatment_overridden", "possible_gift_in_non_annotated_document",
    "footer_id_disagrees_with_boundary", "possible_multiple_matters", "type_unknown", "low_confidence",
)


class TriageError(RuntimeError):
    """Triage could not run (bad input or configuration). Messages carry no document text."""


@dataclass(frozen=True)
class Flag:
    code: str
    document: str | None        # document id, or None for the whole packet
    detail: str                 # ids, sheets and counts only


@dataclass
class TriagedDocument:
    id: str
    file_id: str
    first_sheet: int
    last_sheet: int
    doc_type: str
    title: str
    principal: str
    date: date | None
    refers_to: list[dict]
    appears_to_gift_to_university: str
    is_blank_form: bool
    treatment: str
    treatment_reason: str
    confidence: str
    evidence: list[str]
    model_suggested_treatment: str | None = None
    model_treatment_reason: str | None = None
    pages: list[PageRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat() if self.date else None
        d["pages"] = [{"sheet": p.position, "label": p.label, "label_source": p.label_source}
                      for p in self.pages]
        return d


@dataclass
class TriageResult:
    documents: list[TriagedDocument]
    flags: list[Flag]
    warnings: list[str]
    retries_used: int
    needs_attention: bool
    usage: dict

    def to_dict(self) -> dict:
        return {
            "documents": [d.to_dict() for d in self.documents],
            "flags": [asdict(f) for f in self.flags],
            "warnings": list(self.warnings),
            "retries_used": self.retries_used,
            "needs_attention": self.needs_attention,
            "usage": dict(self.usage),
        }


# ---------------------------------------------------------------------------
# Default treatments
# ---------------------------------------------------------------------------
def load_treatments(path: Path) -> dict[str, str]:
    """The table as {doc_type: treatment}. Every type must appear exactly once."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for treatment in TREATMENTS:
        for doc_type in raw.get(treatment, []):
            if doc_type not in DOC_TYPES or doc_type in table:
                raise TriageError(f"{Path(path).name}: type {doc_type!r} is unknown or listed twice")
            table[doc_type] = treatment
    missing = [t for t in DOC_TYPES if t not in table]
    if missing:
        raise TriageError(f"{Path(path).name}: no default treatment for {', '.join(missing)}")
    return table


# ---------------------------------------------------------------------------
# Claims: one model document, normalized, before ids and treatments are assigned
# ---------------------------------------------------------------------------
@dataclass
class _Claim:
    file_id: str
    first: int
    last: int
    doc: TriageDocument
    date: date | None
    refers_to: list[dict]
    flags: list[str] = field(default_factory=list)     # codes, attached once the id is known


def _unassigned_claim(file_id: str, first: int, last: int) -> _Claim:
    doc = TriageDocument(
        file_id=file_id, first_sheet=first, last_sheet=last, doc_type="unknown", title="unknown",
        principal="unknown", date="unknown", refers_to=[], appears_to_gift_to_university="unclear",
        is_blank_form=False, proposed_treatment="log_only",
        treatment_reason="no document claimed these sheets", confidence="low", evidence=[])
    return _Claim(file_id, first, last, doc, None, [], ["unassigned"])


def _runs(sheets: list[int]) -> list[tuple[int, int]]:
    """Contiguous runs in a sorted list of sheet numbers."""
    runs: list[tuple[int, int]] = []
    for s in sheets:
        if runs and s == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], s)
        else:
            runs.append((s, s))
    return runs


def _span(a: int, b: int) -> str:
    return f"sheet {a}" if a == b else f"sheets {a}-{b}"


def _validate(parsed, problems: list[str], shown: dict[str, tuple[int, int]]) -> list[str]:
    """All problems with one answer, by position and count only."""
    problems = list(problems)
    claimed: dict[str, dict[int, int]] = {f: {} for f in shown}
    for index, doc, bad_type in parsed:
        if bad_type:
            continue                                   # already reported by parse_documents
        if doc.file_id not in shown:
            problems.append(f"document {index}: file_id is not one of the files shown")
            continue
        lo, hi = shown[doc.file_id]
        if doc.first_sheet > doc.last_sheet:
            problems.append(f"document {index}: first_sheet is after last_sheet")
            continue
        if doc.first_sheet < lo or doc.last_sheet > hi:
            problems.append(f"document {index}: range is outside {doc.file_id} sheets {lo}-{hi}")
        if not parse_date(doc.date)[1]:
            problems.append(f"document {index}: date is not YYYY-MM-DD or 'unknown'")
        if any(not parse_date(r.date)[1] for r in doc.refers_to):
            problems.append(f"document {index}: a refers_to date is not YYYY-MM-DD or 'unknown'")
        for s in range(max(lo, doc.first_sheet), min(hi, doc.last_sheet) + 1):
            claimed[doc.file_id][s] = claimed[doc.file_id].get(s, 0) + 1
    for file_id, (lo, hi) in shown.items():
        counts = claimed[file_id]
        for a, b in _runs([s for s in range(lo, hi + 1) if counts.get(s, 0) == 0]):
            problems.append(f"{file_id} {_span(a, b)}: not in any document ({b - a + 1} sheets)")
        for a, b in _runs([s for s in range(lo, hi + 1) if counts.get(s, 0) > 1]):
            problems.append(f"{file_id} {_span(a, b)}: in more than one document ({b - a + 1} sheets)")
    return problems


def _to_claim(doc: TriageDocument, first: int, last: int, flags: list[str]) -> _Claim:
    day, ok = parse_date(doc.date)
    refers, refers_ok = [], True
    for r in doc.refers_to:
        r_day, r_ok = parse_date(r.date)
        refers_ok &= r_ok
        refers.append({"what": r.what, "date": r_day.isoformat() if r_day else None})
    flags = list(flags)
    if not ok or not refers_ok:
        flags.append("date_unreadable")
    return _Claim(doc.file_id, first, last, doc, day, refers, flags)


def _repair(parsed, shown: dict[str, tuple[int, int]]) -> list[_Claim]:
    """Conservative repair of an answer that failed validation twice."""
    taken: dict[str, set[int]] = {f: set() for f in shown}
    claims: list[_Claim] = []
    for _, doc, bad_type in parsed:
        if doc.file_id not in shown or doc.first_sheet > doc.last_sheet:
            continue
        lo, hi = shown[doc.file_id]
        free = [s for s in range(max(lo, doc.first_sheet), min(hi, doc.last_sheet) + 1)
                if s not in taken[doc.file_id]]
        if not free:
            continue
        first, last = _runs(free)[0]                   # the earlier claim keeps the overlap
        taken[doc.file_id].update(range(first, last + 1))
        claims.append(_to_claim(doc, first, last, []))
    for file_id, (lo, hi) in shown.items():
        for a, b in _runs([s for s in range(lo, hi + 1) if s not in taken[file_id]]):
            claims.append(_unassigned_claim(file_id, a, b))
    return claims


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------
def _user_message(previews: list[Preview], shown: dict[str, tuple[int, int]],
                  sheets: dict[str, int], problems: list[str] | None) -> str:
    lines = ["Files and sheets shown in this request:"]
    lines += [f"  - {f}: sheets {lo}-{hi} of {sheets[f]}" for f, (lo, hi) in shown.items()]
    lines += ["", f"Call {TOOL_NAME} exactly once, covering every sheet shown.", "", "Previews:", ""]
    lines.append("\n\n".join(p.text for p in previews))
    if problems:
        lines += ["", f"Your previous answer had these problems. Call {TOOL_NAME} again with a "
                      f"complete, corrected answer:"]
        lines += [f"  - {p}" for p in problems]
    return "\n".join(lines)


def build_request(settings: Settings, user_message: str) -> dict:
    forced = not settings.triage_model.startswith(NO_FORCED_TOOL_CHOICE)
    return dict(
        model=settings.triage_model,
        max_tokens=settings.triage_max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": settings.triage_effort},
        system=[{"type": "text", "text": Path(settings.triage_rules).read_text(encoding="utf-8"),
                 "cache_control": {"type": "ephemeral"}}],
        tools=[tool_definition()],
        tool_choice={"type": "tool", "name": TOOL_NAME} if forced else {"type": "auto"},
        messages=[{"role": "user", "content": user_message}],
    )


def _add_usage(usage: dict, response) -> None:
    u = getattr(response, "usage", None)
    usage["calls"] += 1
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        usage[key] += int(getattr(u, key, 0) or 0)


def _call(client, settings: Settings, message: str, usage: dict, debug_name: str):
    """One request. Returns (raw tool input or None, problems with the response itself)."""
    with client.messages.stream(**build_request(settings, message)) as stream:
        response = stream.get_final_message()
    _add_usage(usage, response)
    blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"
              and getattr(b, "name", None) == TOOL_NAME]
    raw = blocks[0].input if blocks else None
    if settings.debug_dir is not None and raw is not None:
        settings.debug_dir.mkdir(parents=True, exist_ok=True)
        (settings.debug_dir / f"last_raw_triage_{debug_name}.json").write_text(json.dumps(raw, indent=2))
    if response.stop_reason == "max_tokens":
        return raw, ["the answer was cut off at max_tokens; keep evidence and reasons short"]
    if raw is None:
        return None, [f"no {TOOL_NAME} call was made"]
    return raw, []


def _triage_window(client, settings, previews, sheets, usage, name) -> tuple[list[_Claim], int, bool]:
    """Claims covering exactly the sheets in these previews. Returns (claims, retries, repaired)."""
    shown: dict[str, tuple[int, int]] = {}
    for p in previews:
        lo, hi = shown.get(p.file_id, (p.sheet, p.sheet))
        shown[p.file_id] = (min(lo, p.sheet), max(hi, p.sheet))

    raw, problems = _call(client, settings, _user_message(previews, shown, sheets, None), usage, f"{name}_1")
    parsed, parse_problems = parse_documents(raw) if raw is not None else ([], [])
    problems = _validate(parsed, problems + parse_problems, shown) if raw is not None else problems
    if not problems:
        return [_to_claim(d, d.first_sheet, d.last_sheet, []) for _, d, _ in parsed], 0, False

    raw, retry_problems = _call(client, settings, _user_message(previews, shown, sheets, problems),
                                usage, f"{name}_2")
    parsed, parse_problems = parse_documents(raw) if raw is not None else ([], [])
    retry_problems = _validate(parsed, retry_problems + parse_problems, shown)
    if not retry_problems:
        return [_to_claim(d, d.first_sheet, d.last_sheet, []) for _, d, _ in parsed], 1, False
    return _repair(parsed, shown), 1, True


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _windows(n: int, size: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """(start, end, own_start, own_end) over the flattened page list, ends exclusive. Each
    window owns its sheets up to the midpoint of the overlap with each neighbour."""
    if n <= size:
        return [(0, n, 0, n)]
    step = size - overlap
    starts = list(range(0, n - overlap, step))
    if starts[-1] + size < n:
        starts.append(n - size)
    out = []
    for k, start in enumerate(starts):
        end = min(n, start + size)
        own_start = 0 if k == 0 else (start + min(n, starts[k - 1] + size)) // 2
        own_end = n if k == len(starts) - 1 else (starts[k + 1] + end) // 2
        out.append((start, end, own_start, own_end))
    return out


def _stitch(per_window, index_of) -> list[_Claim]:
    """Clip each window's claims to the sheets it owns, then rejoin a document cut at an edge:
    same file, same type, adjacent, and one window's unclipped claim actually ran across that
    edge (so two separate same-type documents meeting at the edge stay separate)."""
    out: list[_Claim] = []
    crossed_end: list[bool] = []          # did out[i]'s unclipped claim run past its window's edge?
    for claims, own_start, own_end in per_window:
        first_in_window = True
        for claim in sorted(claims, key=lambda c: index_of[(c.file_id, c.first)]):
            g_first, g_last = index_of[(claim.file_id, claim.first)], index_of[(claim.file_id, claim.last)]
            a, b = max(g_first, own_start), min(g_last, own_end - 1)
            if a > b:
                continue
            clipped = replace(claim, first=claim.first + (a - g_first), last=claim.last - (g_last - b))
            if (first_in_window and out and a == own_start
                    and out[-1].file_id == clipped.file_id and out[-1].last + 1 == clipped.first
                    and out[-1].doc.doc_type == clipped.doc.doc_type
                    and (g_first < own_start or crossed_end[-1])):
                out[-1] = replace(out[-1], last=clipped.last, flags=sorted(set(out[-1].flags + clipped.flags)))
                crossed_end[-1] = g_last >= own_end
            else:
                out.append(clipped)
                crossed_end.append(g_last >= own_end)
            first_in_window = False
    return out


# ---------------------------------------------------------------------------
# Cross-checks
# ---------------------------------------------------------------------------
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def normalize_principal(name: str) -> str | None:
    tokens = [t for t in re.sub(r"[^\w\s]", " ", name.lower()).split() if t not in _SUFFIX]
    if not tokens or tokens == ["unknown"]:
        return None
    return " ".join(sorted(tokens))


def _cross_checks(documents: list[TriagedDocument], features, unassigned: set[str]) -> list[Flag]:
    """Flags only; nothing here changes the map. An unassigned placeholder already needs review,
    so it does not also get gift or confidence flags."""
    flags: list[Flag] = []
    for d in documents:
        placeholder = d.id in unassigned
        if (d.appears_to_gift_to_university in ("yes", "unclear") and d.treatment != "annotate"
                and not placeholder):
            flags.append(Flag("possible_gift_in_non_annotated_document", d.id,
                              f"gift answer {d.appears_to_gift_to_university}, treatment {d.treatment}"))
        if d.doc_type == "unknown":
            flags.append(Flag("type_unknown", d.id, f"{d.file_id} {_span(d.first_sheet, d.last_sheet)}"))
        if d.confidence == "low" and not placeholder:
            flags.append(Flag("low_confidence", d.id, f"{d.file_id} {_span(d.first_sheet, d.last_sheet)}"))
        numbers = {n for s in range(d.first_sheet, d.last_sheet + 1)
                   for n in features[(d.file_id, s)].doc_numbers}
        if len(numbers) > 1:
            flags.append(Flag("footer_id_disagrees_with_boundary", d.id,
                              f"{len(numbers)} different footer document numbers inside the document"))
    for before, after in zip(documents, documents[1:]):
        if before.file_id != after.file_id:
            continue
        shared = (set(features[(before.file_id, before.last_sheet)].doc_numbers)
                  & set(features[(after.file_id, after.first_sheet)].doc_numbers))
        if shared:
            flags.append(Flag("footer_id_disagrees_with_boundary", after.id,
                              f"{before.file_id} sheets {before.last_sheet} and {after.first_sheet} share a "
                              f"footer document number across a boundary"))
    principals = {p for p in (normalize_principal(d.principal) for d in documents) if p}
    if len(principals) > 1:
        flags.append(Flag("possible_multiple_matters", None, f"{len(principals)} distinct principals"))
    return flags


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def triage(pages_by_file: dict[str, list[PageRecord]], settings: Settings, client=None) -> TriageResult:
    if not pages_by_file or not all(pages_by_file.values()):
        raise TriageError("triage needs at least one page in every file")
    for file_id, pages in pages_by_file.items():
        if [p.position for p in pages] != list(range(1, len(pages) + 1)):
            raise TriageError(f"{file_id}: pages must be given in sheet order, starting at 1")
    table = load_treatments(settings.default_treatments)
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=require_api_key(settings))

    sheets = {f: len(p) for f, p in pages_by_file.items()}
    previews = build_previews(pages_by_file, max_chars=settings.triage_preview_chars)
    features = {(p.file_id, p.sheet): p.features for p in previews}
    index_of = {(p.file_id, p.sheet): i for i, p in enumerate(previews)}
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "preview_tokens_estimate": estimate_tokens(previews)}
    warnings: list[str] = []

    windows = _windows(len(previews), settings.triage_window_pages, settings.triage_window_overlap)
    if len(windows) > 1:
        warnings.append(f"{len(previews)} sheets triaged in {len(windows)} overlapping windows "
                        f"of up to {settings.triage_window_pages} sheets")
    per_window, retries, repaired = [], 0, False
    for k, (start, end, own_start, own_end) in enumerate(windows, start=1):
        claims, r, rep = _triage_window(client, settings, previews[start:end], sheets, usage, f"w{k}")
        retries += r
        repaired |= rep
        if rep:
            warnings.append(f"window {k}: answer still invalid after one retry; repaired, needs review")
        per_window.append((claims, own_start, own_end))
    claims = _stitch(per_window, index_of)
    file_order = list(pages_by_file)
    claims.sort(key=lambda c: (file_order.index(c.file_id), c.first))

    documents, flags = [], []
    for n, c in enumerate(claims, start=1):
        doc_id = f"d{n}"
        default = "log_only" if c.doc.is_blank_form else table[c.doc.doc_type]
        overridden = c.doc.proposed_treatment != default
        reason = ("blank form" if c.doc.is_blank_form else f"default for {c.doc.doc_type}") \
            if overridden else c.doc.treatment_reason
        labelled, label_warnings = label_document(pages_by_file[c.file_id][c.first - 1:c.last])
        warnings += [f"{doc_id}: {w}" for w in label_warnings]
        documents.append(TriagedDocument(
            id=doc_id, file_id=c.file_id, first_sheet=c.first, last_sheet=c.last,
            doc_type=c.doc.doc_type, title=c.doc.title, principal=c.doc.principal, date=c.date,
            refers_to=c.refers_to, appears_to_gift_to_university=c.doc.appears_to_gift_to_university,
            is_blank_form=c.doc.is_blank_form, treatment=default, treatment_reason=reason,
            confidence=c.doc.confidence, evidence=clean_evidence(c.doc.evidence),
            model_suggested_treatment=c.doc.proposed_treatment if overridden else None,
            model_treatment_reason=c.doc.treatment_reason if overridden else None,
            pages=labelled))
        span = f"{c.file_id} {_span(c.first, c.last)}"
        for code in c.flags:
            flags.append(Flag(code, doc_id, span))
        if overridden:
            flags.append(Flag("treatment_overridden", doc_id,
                              f"model proposed {c.doc.proposed_treatment}, table gives {default}"))
    flags += _cross_checks(documents, features, {f.document for f in flags if f.code == "unassigned"})
    return TriageResult(documents, flags, warnings, retries, repaired, usage)
