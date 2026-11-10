"""
Run REAL triage (a paid model call) on files and print the document map.

    .venv/bin/python tools/live_triage.py FILE [FILE ...] [--expected expected_map.json]

Pages come through the page-source seam (page_source.load_pages): the PDF text layer, else a
sidecar <file>.pages.json next to the file (this is a dev tool, so sidecars are allowed). Each
file's id is its file name, which is what expected_map.json uses.

Prints id, file, sheets, type, date, treatment, confidence, revoked dates and flags per
document, then the chains built from that map (ids and statuses only). With --expected
it also prints a mismatch table, boundaries first, and exits 1 on any mismatch. It never prints
page text or text the model returned (titles, names, reasons, evidence).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from page_reader import ExtractionError  # noqa: E402
from page_source import load_pages  # noqa: E402
from settings import load_local_env, load_settings  # noqa: E402


def compare_to_map(result, expected: dict) -> list[tuple[str, str, str, str, str]]:
    """Mismatches as (kind, document, field, expected, got), boundaries first."""
    got = {(d.file_id, d.first_sheet, d.last_sheet): d for d in result.documents}
    boundary, fields = [], []
    matched = set()
    for e in expected["documents"]:
        key = (e["file"], e["first"], e["last"])
        d = got.get(key)
        if d is None:
            covering = [x for x in result.documents if x.file_id == e["file"]
                        and x.first_sheet <= e["first"] <= x.last_sheet]
            have = f"{covering[0].first_sheet}-{covering[0].last_sheet}" if covering else "none"
            boundary.append(("boundary", e["id"], "sheets", f"{e['first']}-{e['last']}", have))
            continue
        matched.add(key)
        for name, want, have in (("type", e["type"], d.doc_type),
                                 ("date", e["date"], d.date.isoformat() if d.date else None),
                                 ("treatment", e["treatment"], d.treatment)):
            if want != have:
                fields.append(("field", e["id"], name, str(want), str(have)))
    for key, d in got.items():
        if key not in matched:
            boundary.append(("boundary", f"(extra {d.id})", "sheets", "none",
                             f"{d.file_id} {d.first_sheet}-{d.last_sheet}"))
    counts = expected.get("expected_counts", {})
    computed = {"documents": len(result.documents),
                **{t: sum(d.treatment == t for d in result.documents) for t in ("annotate", "facts_only", "log_only")}}
    count_rows = [("count", "-", k, str(v), str(computed[k])) for k, v in counts.items()
                  if k in computed and computed[k] != v]
    return boundary + fields + count_rows


def table(rows: list[tuple], header: tuple) -> str:
    rows = [tuple(str(c) for c in header), *[tuple(str(c) for c in r) for r in rows]]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    return "\n".join("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--expected", type=Path, help="expected_map.json to compare against")
    args = parser.parse_args(argv)

    load_local_env()
    settings = load_settings()
    from triage import triage

    pages_by_file = {}
    for path in args.files:
        try:
            pages_by_file[path.name] = load_pages(path.name, path.name, path.read_bytes(),
                                                  sidecar_dir=path.parent, max_pages=settings.max_pages)
        except ExtractionError as exc:
            print(f"cannot read {path.name}: {exc.kind} (needs a text layer or {path.name}.pages.json)")
            return 2

    result = triage(pages_by_file, settings)
    flags_by_doc: dict[str, list[str]] = {}
    for f in result.flags:
        flags_by_doc.setdefault(f.document or "-", []).append(f.code)
    print(table([(d.id, d.file_id, f"{d.first_sheet}-{d.last_sheet}", d.doc_type,
                  d.date.isoformat() if d.date else "-", d.treatment, d.confidence,
                  ", ".join(r["date"] or "undated" for r in d.revokes) or "-",
                  ", ".join(flags_by_doc.get(d.id, [])) or "-") for d in result.documents],
                ("id", "file", "sheets", "type", "date", "treatment", "confidence", "revokes", "flags")))

    from chains import build_chains, from_triaged
    chained = build_chains([from_triaged(d, order=i) for i, d in enumerate(result.documents)])
    print("\nchains (ids and statuses only):")
    for c in chained.chains:
        members = ", ".join(f"{m.id}:{m.status}{'+revival' if m.revival_assumed else ''}" for m in c.members)
        print(f"  {c.id} {c.family}: base {c.base}, amendments {c.amendments}; {members}; gaps {len(c.gaps)}")
    for f in chained.flags:
        print(f"  chain flag: {f.code} {f.document or '-'}")
    if "-" in flags_by_doc:
        print(f"\npacket flags: {', '.join(flags_by_doc['-'])}")
    print(f"\nretries {result.retries_used}, needs_attention {result.needs_attention}, "
          f"usage {json.dumps(result.usage)}")
    for w in result.warnings:
        print(f"warning: {w}")

    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        rows = compare_to_map(result, expected)
        if rows:
            print("\nMISMATCHES (boundaries first; the code or the expected map may be wrong):")
            print(table(rows, ("kind", "document", "field", "expected", "got")))
            return 1
        print("\nmatches the expected map")
    return 0


if __name__ == "__main__":
    sys.exit(main())
