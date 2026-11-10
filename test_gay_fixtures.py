"""
Checks on the real (scanned) fixtures in fixtures/gay/ and their hand-written expectations
(docs/ACCEPTANCE.md, rung 5 and the structure every rung relies on).

    .venv/bin/python test_gay_fixtures.py

Structure checks always run: the expected files agree with the PDFs and with themselves.
Text checks label each document's sheets and compare with expected_map.json "label_rules". They
need page text, so they SKIP for a file with no text layer and no sidecar
(fixtures/gay/<filename>.pages.json, see page_source.py). Skips are not failures.

A live triage check (a paid model call) runs only when every file has text or a sidecar AND
CANETRUST_LIVE=1 is set (ANTHROPIC_API_KEY must be exported: this script never reads .env).

This is not an OCR test: it reads whatever text page_source gives it. The expected files were
written by hand from page images, so a mismatch can be a wrong expectation. Report it; never
edit expected_map.json just to make this pass. No API key, no network.
"""

import json
import os
import sys
from pathlib import Path

os.environ["CANETRUST_DISABLE_DOTENV"] = "1"

from pypdf import PdfReader

from extract import ExtractionError
from page_labels import label_document
from page_source import load_pages, sidecar_path

GAY = Path(__file__).parent / "fixtures" / "gay"
TREATMENTS = {"annotate", "facts_only", "log_only"}
passed = 0


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


def load(name):
    return json.loads((GAY / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Structure checks
# ---------------------------------------------------------------------------
expected_map = load("expected_map.json")
files, documents = expected_map["files"], expected_map["documents"]

for name, info in files.items():
    path = GAY / name
    check(path.is_file(), f"{name} exists in fixtures/gay/")
    check(len(PdfReader(path).pages) == info["sheets"], f"{name}: {info['sheets']} sheets, as in the map")

ids = [d["id"] for d in documents]
check(len(ids) == len(set(ids)), "document ids are unique")
for d in documents:
    check(d["file"] in files, f"{d['id']}: its file is listed in 'files'")
    check(1 <= d["first"] <= d["last"] <= files[d["file"]]["sheets"], f"{d['id']}: range inside its file")
    check(d["treatment"] in TREATMENTS, f"{d['id']}: treatment {d['treatment']!r} is known")
    if "amends" in d:
        check(d["amends"] in ids, f"{d['id']}: amends {d['amends']!r}, an existing id")

for name, info in files.items():
    covered = sorted(s for d in documents if d["file"] == name for s in range(d["first"], d["last"] + 1))
    check(covered == list(range(1, info["sheets"] + 1)),
          f"{name}: documents cover every sheet exactly once (no gaps or overlaps)")


def expected_labels(rule):
    """Per-sheet (label, source) for an asserted rule."""
    first, last = rule["sheets"]
    if "labels" in rule:
        labels = rule["labels"]
    else:
        labels = [str(rule["labels_from"] + k) for k in range(last - first + 1)]
    return {sheet: (label, rule["source"]) for sheet, label in zip(range(first, last + 1), labels)}


for d in documents:
    rule_sheets = []
    for rule in d["label_rules"]:
        first, last = rule["sheets"]
        check(d["first"] <= first <= last <= d["last"], f"{d['id']}: rule {first}-{last} stays in its range")
        rule_sheets += range(first, last + 1)
        if rule.get("assert", True):
            check(("labels" in rule) != ("labels_from" in rule) and "source" in rule,
                  f"{d['id']}: asserted rule {first}-{last} gives labels or labels_from, and a source")
            if "labels" in rule:
                check(len(rule["labels"]) == last - first + 1,
                      f"{d['id']}: rule {first}-{last} lists one label per sheet")
    check(sorted(rule_sheets) == list(range(d["first"], d["last"] + 1)),
          f"{d['id']}: label_rules cover each of its sheets exactly once")

for chain in expected_map["chains"]:
    check(all(ref in ids for ref in [chain["base"], *chain["amendments"]]),
          f"chain {chain['id']}: base and amendments are existing ids")

counts = expected_map["expected_counts"]
computed = {"documents": len(documents), "chains": len(expected_map["chains"]),
            **{t: sum(d["treatment"] == t for d in documents) for t in TREATMENTS}}
check(counts == computed, f"expected_counts match the documents: {counts} vs {computed}")

facts = load("expected_facts.json")
for key in ("date_of_death", "probate_case", "personal_representative", "successor_trustee",
            "addresses_for_mahalie_ann_fulton", "expected_conflict", "estate_attorney"):
    check(key in facts, f"expected_facts.json has {key!r}")
addresses = facts["addresses_for_mahalie_ann_fulton"]
check(len({a["address"] for a in addresses}) == 2, "expected_facts.json plants two differing addresses")
for key, value in facts.items():
    for source in value.get("sources", []) if isinstance(value, dict) else []:
        check(source.split(" ")[0] in ids, f"expected_facts.json {key}: source {source!r} is a document id")

findings = load("expected_findings.json")
for key in ("trust_chain", "will_chain", "accounting"):
    check(key in findings, f"expected_findings.json has {key!r}")

workbook = GAY / expected_map["ground_truth_workbook"]["file"]
if not workbook.is_file():
    print(f"NOTE: {workbook.relative_to(GAY.parent.parent)} is not present yet (needed for "
          f"acceptance rung 1, not by this script)")

structure_passed = passed

# ---------------------------------------------------------------------------
# Text checks: printed-page labels per document
# ---------------------------------------------------------------------------
pages_by_file, skip_reason = {}, {}
for name in files:
    try:
        pages_by_file[name] = load_pages(name, name, (GAY / name).read_bytes(), sidecar_dir=GAY)
    except ExtractionError as exc:
        if exc.kind != "no_text_layer":
            raise
        skip_reason[name] = f"no text layer and no sidecar ({sidecar_path(GAY, name).name})"

text_passed, text_skipped, mismatches = 0, 0, []
for d in documents:
    expected = {}
    for rule in d["label_rules"]:
        if rule.get("assert", True):
            expected.update(expected_labels(rule))
    if not expected:
        continue
    if d["file"] in skip_reason:
        text_skipped += 1
        print(f"SKIP {d['id']}: {d['file']}: {skip_reason[d['file']]}")
        continue
    labelled, _ = label_document(pages_by_file[d["file"]][d["first"] - 1:d["last"]])
    got = {d["first"] + i: (r.label, r.label_source) for i, r in enumerate(labelled)}
    bad = [(d["id"], s, *expected[s], *got[s]) for s in sorted(expected) if got[s] != expected[s]]
    mismatches += bad
    text_passed += not bad

if mismatches:
    header = ("document", "sheet", "expected label", "source", "got label", "source")
    rows = [header, *[tuple(str(x) for x in row) for row in mismatches]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    print("\nLABEL MISMATCHES (the code or expected_map.json may be wrong):")
    for row in rows:
        print("  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths)))

# ---------------------------------------------------------------------------
# Live triage check: skipped unless the files are readable AND CANETRUST_LIVE=1
# ---------------------------------------------------------------------------
live_rows, live_status = [], "skipped"
if skip_reason:
    print("SKIP live triage: needs a text layer or sidecars for every file")
elif os.environ.get("CANETRUST_LIVE") != "1":
    print("SKIP live triage: set CANETRUST_LIVE=1 to make the paid model call")
else:
    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    from live_triage import compare_to_map, table
    from settings import load_settings
    from triage import triage

    result = triage(pages_by_file, load_settings())
    live_rows = compare_to_map(result, expected_map)
    live_status = "failed" if live_rows else "passed"
    if live_rows:
        print("\nTRIAGE MISMATCHES (boundaries first; the code or expected_map.json may be wrong):")
        print(table(live_rows, ("kind", "document", "field", "expected", "got")))

print(f"\n{structure_passed} structure checks passed, {text_passed} text checks passed, "
      f"{len({m[0] for m in mismatches})} text checks failed, "
      f"{text_skipped} text checks skipped (needs OCR or sidecars), live triage check {live_status}")
sys.exit(1 if mismatches or live_rows else 0)
