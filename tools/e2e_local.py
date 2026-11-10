"""
One REAL end-to-end run (paid model calls) on the five fictional Marlowe PDFs in fixtures/.

    .venv/bin/python tools/e2e_local.py

Runs the same service functions as the web app: load_files -> triage_packet -> auto-confirm ->
annotate_plan, with the real API (ANTHROPIC_API_KEY from .env). Writes the workbook to
scratchpad/e2e_marlowe.xlsx, reopens it with openpyxl and prints: the sheet names, the packet
map (file, sheets, id, type, date, treatment, chain, flags; not titles), the chains, the page
labels cited on each chain sheet and whether each exists, the notes, the timings, and the call
count and token usage.

Only the Marlowe fixtures are accepted: real calls on anything else are not allowed here.
"""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

from plan import chain_plan, label_plan  # noqa: E402
from service import UploadedFile, annotate_plan, auto_confirm_problem, load_files, triage_packet  # noqa: E402
from settings import load_local_env, load_settings, require_api_key  # noqa: E402
from workbook import PACKET_MAP_SHEET  # noqa: E402

FILES = ["00_marlowe_trust_original.pdf", "01_marlowe_first_amendment.pdf", "02_marlowe_second_amendment.pdf",
         "03_marlowe_third_amendment_and_restatement.pdf", "04_marlowe_fourth_amendment.pdf"]
OUT = ROOT / "scratchpad" / "e2e_marlowe.xlsx"
PAGE_RE = re.compile(r"\b(?:pp?\.|pages?)\s*(unnumbered,\s*sheet\s+\d+\s+of\s+\d+|\d{1,4}|[ivxlcdm]{1,8})"
                     r"(?:\s*[-–]\s*(\d{1,4}|[ivxlcdm]{1,8}))?(?![\w])", re.I)
CITATION_COLS = "CGHIJKLMNQ"


def main() -> int:
    load_local_env()
    settings = load_settings()
    require_api_key(settings)
    files = [UploadedFile(name, (ROOT / "fixtures" / name).read_bytes()) for name in FILES]
    progress = lambda m: print(f"[progress] {m}")       # noqa: E731

    loaded = load_files(files, settings)
    started = time.monotonic()
    outcome = triage_packet(loaded, settings, progress, name="Marlowe e2e")
    triage_seconds = time.monotonic() - started
    plan = outcome.plan
    print("\nDocument map:")
    for d in plan.documents:
        flags = ", ".join(f["code"] for f in d.flags) or "-"
        print(f"  {d.id}  {d.file_id}  sheets {d.first_sheet}-{d.last_sheet}  {d.doc_type}  "
              f"{d.date or 'undated'}  {d.treatment}  confidence={d.confidence}  flags={flags}")
    for f in plan.flags:
        print(f"  packet flag: {f['code']}: {f['detail']}")
    problem = auto_confirm_problem(plan)
    if problem:
        print(f"\n[stopped] {problem}")
        print(f"Triage usage: {outcome.usage}")
        return 1

    started = time.monotonic()
    result = annotate_plan(plan, loaded.pages_by_file, settings, progress)
    annotate_seconds = time.monotonic() - started
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(result.xlsx)
    print(f"\nWrote {OUT.relative_to(ROOT)}")

    wb = load_workbook(io.BytesIO(result.xlsx))
    print(f"\nSheets: {wb.sheetnames}")
    pm = wb[PACKET_MAP_SHEET]
    print("\nPacket map (title and reason columns not printed):")
    for r in range(3, pm.max_row + 1):
        row = [pm.cell(row=r, column=c).value for c in range(1, 11)]
        if not any(row):
            continue
        if row[0] == "Chains":
            break
        file, sheets, doc_id, doc_type, _title, day, treatment, _reason, chain, flags = row
        print(f"  {file} | {sheets} | {doc_id} | {doc_type} | {day} | {treatment} | {chain} | "
              f"{(flags or '').replace(chr(10), '; ')}")
    print("\nChains:")
    for c in result.chains:
        print(f"  {c['id']} {c['family']}: sheet={c['sheet']!r} base={c['base'].split(' (')[0]} "
              f"amendments={c['amendments']} -> {c['result']}")

    label_plan(plan, loaded.pages_by_file)
    labels = {d.id: {(p.label or '').lower() for p in d.pages} for d in plan.documents}
    print("\nPage labels cited (checked against every instrument of the chain):")
    chains = chain_plan(plan).chains
    for c, ws_name in zip(chains, [x["sheet"] for x in result.chains]):
        if ws_name not in wb.sheetnames:
            continue
        ws = wb[ws_name]
        known = set().union(*(labels[i] for i in c.instruments)) if c.instruments else set()
        cited = []
        for row in ws.iter_rows(min_row=13):
            for cell in row:
                if cell.column_letter in CITATION_COLS and isinstance(cell.value, str):
                    for m in PAGE_RE.finditer(cell.value):
                        cited += [x.lower() for x in m.groups() if x]
        unknown = sorted({x for x in cited if x not in known})
        print(f"  {ws_name!r}: {len(cited)} citation(s), labels {sorted(set(cited))}; "
              f"labels not in the chain: {unknown or 'none'}")
        notes = ws["B3"].value
        if notes:
            print(f"    chain notes: {len(notes.splitlines())} line(s)")
            for line in notes.splitlines():
                if line.startswith(("Cited page not found", "NOT PROVIDED", "No base", "Revival")):
                    print(f"      {line}")

    print("\nWarnings:")
    for w in result.warnings or ["(none)"]:
        print(f"  {w}")
    total = {k: outcome.usage.get(k, 0) + result.usage.get(k, 0) for k in ("calls", "input_tokens", "output_tokens")}
    print(f"\nTimings: triage {triage_seconds:.1f} s, annotation (chains + facts, parallel) {annotate_seconds:.1f} s")
    print(f"Usage: triage {outcome.usage}")
    print(f"       annotation + facts {result.usage}")
    print(f"       total: {total['calls']} call(s), input {total['input_tokens']} tokens, "
          f"output {total['output_tokens']} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
