"""
API-free tests for the page reader and the printed-page labeler (docs/DESIGN.md, milestone 1).

    .venv/bin/python test_pages.py

Unit tests run on plain lists of page strings with INVENTED text. The end-to-end tests use the
FICTIONAL Marlowe PDFs in fixtures/. No real documents, no API key, no network.
"""

import io
import json
import os
import tempfile
from pathlib import Path

os.environ["CANETRUST_DISABLE_DOTENV"] = "1"

from pypdf import PdfReader, PdfWriter

from page_labels import find_candidates, find_label_candidate, label_document
from page_reader import ExtractionError, PageRecord, read_pages
from page_source import load_pages, sidecar_path

FIX = Path(__file__).parent / "fixtures"
passed = 0
all_warnings: list[str] = []

# Every invented page carries this word, so a warning that leaks page text is caught.
MARKER = "Quillfeather"
# Stray bare numbers ("2", "4": list items) sit in the middle, outside the first 2 and last 6 lines.
BODY = [
    f"The {MARKER} trustee shall hold the property described in Section 7 of this instrument",
    "and shall distribute the income at least quarterly to the beneficiaries named below.",
    "2",
    "The trustee may retain any asset received, including 3 parcels of land.",
    "4",
    "The trustee may sell any asset on terms the trustee thinks proper.",
    "No beneficiary may assign or encumber any interest before it is distributed.",
    "Any power given to the trustee may be exercised without the approval of any court.",
    "Each provision of this instrument is severable from every other provision.",
    "The trustee shall keep accurate books of account for the trust.",
    "This instrument is governed by the law of the state named in Article 9.",
]


def check(condition, message):
    global passed
    assert condition, f"FAILED: {message}"
    passed += 1


def page(*footer, header=("Declaration of the Example Trust",)):
    """One page: header lines, a body whose stray numbers sit outside the candidate window,
    then the footer lines given."""
    return "\n".join([*header, *BODY, *footer])


def doc(*texts):
    return [PageRecord("test-file", position, text) for position, text in enumerate(texts, start=1)]


def run(*texts):
    pages, warnings = label_document(doc(*texts))
    all_warnings.extend(warnings)
    return [(p.label, p.label_source) for p in pages], warnings


def labels(*texts):
    return [label for label, _ in run(*texts)[0]]


def sources(*texts):
    return [source for _, source in run(*texts)[0]]


def unnumbered(n):
    return [f"unnumbered, sheet {i} of {n}" for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# 1. Candidate patterns
# ---------------------------------------------------------------------------
check(labels(page("- 7 -")) == ["7"], "'- 7 -' counts on its own")
check(labels(page("Page 7")) == ["7"], "'Page 7' counts on its own")
check(labels(page("Page 7 of 41")) == ["7"], "'Page 7 of 41' counts on its own")
check(labels(page(f"{MARKER} Trust - Declaration of Trust - Page 2")) == ["2"],
      "'Page N' at the end of a longer footer line")
check(labels(page(header=("Page 3", "Declaration of the Example Trust"))) == ["3"],
      "'Page N' in the header")
check(labels(page("7 of 41")) == ["7"], "'7 of 41' alone on a line counts on its own")
check(labels(page("1 of 2"), page("2 of 2")) == ["1", "2"], "'1 of 2' / '2 of 2'")
check(labels(page("- iv -")) == ["iv"], "'- iv -' gives a roman label")
check(labels(page("Page iv")) == ["iv"], "'Page iv' gives a roman label")
check(labels(page("7"), page("8")) == ["7", "8"], "bare numbers on two agreeing pages")
check(labels(page("Page 9 of 4")) == unnumbered(1), "'Page N of M' with N > M is rejected")
check(labels(page(f"as set out on page 3")) == unnumbered(1), "lower-case 'page 3' in prose is not a label")
check(find_candidates(page(), max_bare=20) == [], "numbers in the body, outside the window, are ignored")
check(find_label_candidate(page("Page 7 of 41"), max_bare=20) == "Page 7 of 41",
      "label_candidate keeps the raw printed text")

# ---------------------------------------------------------------------------
# 2. Sequence rules
# ---------------------------------------------------------------------------
got, warnings = run(page("Page 1"), page("Page 5"), page("Page 3"))
check([l for l, _ in got] == ["1", "2", "3"], "the sequence must climb by one: a stray 5 is out-voted")
check(got[1][1] == "inferred", "the out-of-sequence page is inferred")
check(any("sheet 2" in w and "5 [page]" in w and "sequence_mismatch" in w for w in warnings),
      "the out-of-sequence number is reported with its sheet and rule")

got, _ = run(page("Page 1"), page(), page("Page 3"))
check(got == [("1", "printed"), ("2", "inferred"), ("3", "printed")], "a gap is filled")

got, _ = run(page(), page("Page 2"), page("Page 3"))
check(got == [("1", "inferred"), ("2", "printed"), ("3", "printed")],
      "page 1 prints nothing and page 2 prints 2: page 1 is inferred")
got, _ = run(page(), page("2"), page("3"))
check(got == [("1", "inferred"), ("2", "printed"), ("3", "printed")],
      "the same with bare numbers")

check(labels(page("Page 1"), page("Page 2"), page("Signed by the trustee")) == ["1", "2", "3"],
      "a trailing unnumbered page continues the sequence")
check(sources(page("Page 1"), "", page("Page 3")) == ["printed", "inferred", "printed"],
      "a page with no text is inferred")

got, _ = run(page("Page 5"), page(), page("Page 7"))
check([l for l, _ in got] == ["5", "6", "7"], "an excerpt starting at page 5 keeps its printed numbers, not positions")

got, _ = run(page("EXAMPLE TRUST", header=()), page("Page 1"), page("Page 2"))
check(got == [("unnumbered, sheet 1 of 3", "unnumbered"), ("1", "printed"), ("2", "printed")],
      "a cover page before printed page 1 is unnumbered")

got, _ = run(page(header=("TITLE PAGE",)), page("ii"), page("iii"), page("1"), page("2"), page("3"))
check(got == [("i", "inferred"), ("ii", "printed"), ("iii", "printed"),
              ("1", "printed"), ("2", "printed"), ("3", "printed")],
      "roman front matter is labelled separately from the arabic run")
check(labels(page("- II -"), page("- III -"), page("Page 1")) == ["II", "III", "1"],
      "upper-case roman front matter keeps its case")

got, warnings = run(page(), page(), page())
check(got == [(l, "unnumbered") for l in unnumbered(3)] and not warnings,
      "a document with no printed numbers is 'unnumbered, sheet N of M'")

got, warnings = run(page("1"), page())
check([l for l, _ in got] == unnumbered(2), "one bare number is not enough")
check(any("unconfirmed" in w for w in warnings), "the unconfirmed bare number is reported")
check(labels(page("1")) == unnumbered(1), "a one-page document with only a bare number is unnumbered")
check(labels(page("iv")) == unnumbered(1), "the same for a bare roman numeral")

first = [page("Page 1"), page("Page 2")]
second = [page("Page 1"), page("Page 2"), page("Page 3")]
check(labels(*first) == ["1", "2"] and labels(*second) == ["1", "2", "3"],
      "numbering restarts per document when each is labelled on its own")
got, warnings = run(*first, *second)
check(any(source != "printed" for _, source in got) and any("sequence_mismatch" in w for w in warnings),
      "two documents labelled as one are flagged, not silently numbered")

# ---------------------------------------------------------------------------
# 3. Footer block with a document-system ID under the page number
# ---------------------------------------------------------------------------
FIRM = ("Example Fictional Firm LLP", f"{MARKER} Office, Suite 100")


def firm_page(number=None, doc_id="48213907"):
    footer = ([str(number)] if number is not None else []) + [FIRM[0], FIRM[1], doc_id]
    return page(*footer)


got, warnings = run(firm_page(1), firm_page(2), firm_page(3))
check(got == [("1", "printed"), ("2", "printed"), ("3", "printed")],
      "a bare page number above a three-line footer with an 8-digit ID")
check(not warnings, "the ID raises no warnings")
check(find_label_candidate(firm_page(1), max_bare=13) == "1", "the ID is never the label candidate")
check(labels(firm_page(), firm_page(), firm_page()) == unnumbered(3),
      "the same ID on every page produces no labels")
check(labels(firm_page()) == ["unnumbered, sheet 1 of 1"], "a one-page document with only the ID is unnumbered")
check(labels(firm_page(1)) == ["unnumbered, sheet 1 of 1"],
      "a one-page document with a bare number and the ID is unnumbered")
check(labels(*(firm_page(doc_id="10234") for _ in range(3))) == unnumbered(3),
      "a bare 5-digit number is never a page number")
check(labels(*(firm_page(doc_id="2004") for _ in range(3))) == unnumbered(3),
      "a bare year (larger than the page count + 10) is never a page number")
check(find_candidates(firm_page(doc_id="10234"), max_bare=10**6) == [],
      "the 5-digit rule holds on its own, whatever the page count")
check(labels(page("2003"), page("2004"), page("2005")) == unnumbered(3),
      "climbing bare numbers above the page count + 10 are still not page numbers")

# ---------------------------------------------------------------------------
# 4. Contents page
# ---------------------------------------------------------------------------
CONTENTS = [
    "Article One. Name of the Trust ........................ 1",
    "Article Two. Trust Property . . . . . . . . . . . . . . 3",
    "Article Three. Distribution of the Residue ............",
    "4",
]
got, warnings = run(page("1"), page(*CONTENTS, "2", header=("CONTENTS",)), page("3"), page("4"))
check(got == [(str(n), "printed") for n in range(1, 5)],
      "contents entries (including a wrapped trailing number) do not become labels")
check(not warnings, "contents entries raise no warnings")
got, warnings = run(page("1"), page(*CONTENTS, header=("CONTENTS",)), page("3"), page("4"))
check(got == [("1", "printed"), ("2", "inferred"), ("3", "printed"), ("4", "printed")],
      "a contents page ending in a wrapped entry number is inferred; its neighbours are unaffected")
check(not warnings, "no warnings for the wrapped entry number")
ELLIPSIS = ["Article Four. Powers of the Trustee …", "9"]
check(labels(page("1"), page(*ELLIPSIS, header=("CONTENTS",)), page("3")) == ["1", "2", "3"],
      "an ellipsis leader is a contents entry too")

# ---------------------------------------------------------------------------
# 5. Form-style footers
# ---------------------------------------------------------------------------
got, _ = run(page("Form No. X-1.2.3- 1 of 2"), page("Form No. X-1.2.3- 2 of 2"))
check(got == [("1", "printed"), ("2", "printed")], "form-style 'N of M' at the end of a line")
got, warnings = run(page("Form No. X-1.2.3- 1 of 2"), page("Form No. X-1.2.3- 2 of 3"))
check([l for l, _ in got] == unnumbered(2), "form-style numbers with different totals are ignored")
check(any("form_inconsistent" in w and "totals differ" in w for w in warnings), "...and reported")
got, warnings = run(page("Form No. X-1.2.3- 1 of 2"), page("Form No. X-1.2.3- 1 of 2"))
check([l for l, _ in got] == unnumbered(2), "form-style numbers that do not climb are ignored")
check(any("form_inconsistent" in w and "climb" in w for w in warnings), "...and reported")

# ---------------------------------------------------------------------------
# 6. Warnings never contain document text
# ---------------------------------------------------------------------------
check(all_warnings, "the cases above produced warnings to inspect")
check(not any(MARKER in w or "Trust" in w or "Form No" in w or "Article" in w for w in all_warnings),
      "warnings hold positions, numbers and rule names only")

# ---------------------------------------------------------------------------
# 7. End to end with the fictional Marlowe PDFs
# ---------------------------------------------------------------------------
FILES = [
    ("00_marlowe_trust_original.pdf", 2),
    ("01_marlowe_first_amendment.pdf", 1),
    ("02_marlowe_second_amendment.pdf", 1),
    ("03_marlowe_third_amendment_and_restatement.pdf", 2),
    ("04_marlowe_fourth_amendment.pdf", 1),
]

for name, count in FILES:
    records = read_pages(name, name, (FIX / name).read_bytes())
    check([r.position for r in records] == list(range(1, count + 1)), f"{name}: one record per page")
    check(all(r.label is None and r.label_source is None for r in records),
          f"{name}: the reader leaves labelling to the labeler")
    check([r.label_candidate for r in records] == [f"Page {n}" for n in range(1, count + 1)],
          f"{name}: the printed 'Page N' footers are the candidates")
    labelled, warnings = label_document(records)
    check([(r.label, r.label_source) for r in labelled] == [(str(n), "printed") for n in range(1, count + 1)]
          and not warnings, f"{name}: labels come from the printed numbers")

writer = PdfWriter()
for name, _ in FILES:
    for pdf_page in PdfReader(FIX / name).pages:
        writer.add_page(pdf_page)
buffer = io.BytesIO()
writer.write(buffer)
packet = read_pages("packet", "packet.pdf", buffer.getvalue())
check([r.position for r in packet] == list(range(1, 8)), "a merged packet reads as 7 pages in file order")

start, packet_labels = 0, []
for name, count in FILES:
    labelled, warnings = label_document(packet[start:start + count])
    check(not warnings, f"packet slice for {name}: no warnings")
    packet_labels += [r.label for r in labelled]
    start += count
check(packet_labels == ["1", "2", "1", "1", "1", "2", "1"],
      "per-document labelling restarts at 1 in each document, never the file position")

labelled, warnings = label_document(packet)
check(warnings and not any("Marlowe" in w or "Trust" in w for w in warnings),
      "labelling the whole packet as one document warns, without document text")

def raises_kind(fn, kind):
    try:
        fn()
    except ExtractionError as exc:
        return exc.kind == kind
    return False


check(raises_kind(lambda: read_pages("s", "scan_like_blank.pdf", (FIX / "scan_like_blank.pdf").read_bytes()),
                  "no_text_layer"), "a scan is refused, as in extract.py")
check(raises_kind(lambda: read_pages("d", "notes.docx", b"PK..."), "unsupported"), "unsupported types are refused")
check(raises_kind(lambda: read_pages("e", "empty.pdf", b""), "empty"), "empty files are refused")
check(raises_kind(lambda: read_pages("x", "fake.pdf", b"not a pdf"), "invalid"), "non-PDFs are refused")

txt = read_pages("t", "notes.txt", f"{MARKER} first sheet\nPage 1\f{MARKER} second sheet\nPage 2\f".encode())
check([r.position for r in txt] == [1, 2] and [r.label_candidate for r in txt] == ["Page 1", "Page 2"],
      "a .txt file splits on form feeds")

# ---------------------------------------------------------------------------
# 8. Page source: text layer, then (tests and dev tools only) a sidecar, then refuse
# ---------------------------------------------------------------------------
SCAN = (FIX / "scan_like_blank.pdf").read_bytes()          # 2 sheets, no text layer
TRUST = "00_marlowe_trust_original.pdf"

with tempfile.TemporaryDirectory() as tmp:
    sidecars = Path(tmp)

    def write_sidecar(filename, value):
        sidecar_path(sidecars, filename).write_text(json.dumps(value), encoding="utf-8")

    write_sidecar(TRUST, [f"{MARKER} sidecar\nPage 9", f"{MARKER} sidecar\nPage 10"])
    records = load_pages(TRUST, TRUST, (FIX / TRUST).read_bytes(), sidecar_dir=sidecars)
    check([r.label_candidate for r in records] == ["Page 1", "Page 2"],
          "a text layer wins over a sidecar")

    write_sidecar("scan.pdf", [f"{MARKER} first\n- 1 -", ""])
    records = load_pages("s", "scan.pdf", SCAN, sidecar_dir=sidecars)
    check([(r.file_id, r.position) for r in records] == [("s", 1), ("s", 2)]
          and records[0].label_candidate == "- 1 -" and records[1].text == "",
          "a scan with a sidecar gives one record per sheet, in sheet order")
    check(raises_kind(lambda: load_pages("s", "scan.pdf", SCAN), "no_text_layer"),
          "without sidecar_dir the sidecar is never read (the API path)")
    check(raises_kind(lambda: load_pages("s", "other.pdf", SCAN, sidecar_dir=sidecars), "no_text_layer"),
          "a scan with no sidecar is refused as before")

    write_sidecar("short.pdf", [f"{MARKER} only one"])
    try:
        load_pages("s", "short.pdf", SCAN, sidecar_dir=sidecars)
        message = ""
    except ExtractionError as exc:
        message = str(exc) if exc.kind == "invalid" else ""
    check("1 page texts" in message and "2 sheets" in message and MARKER not in message,
          "a sidecar with the wrong number of pages is refused, without page text")
    write_sidecar("notlist.pdf", {"pages": [f"{MARKER} a", "b"]})
    check(raises_kind(lambda: load_pages("s", "notlist.pdf", SCAN, sidecar_dir=sidecars), "invalid"),
          "a sidecar that is not a list of strings is refused")

# Since milestone 6 the service reads pages through page_source.load_pages, but never with a
# sidecar directory: only tests and dev tools may read sidecars.
for runtime in ("api/index.py", "service.py"):
    source = (Path(__file__).parent / runtime).read_text(encoding="utf-8")
    check("sidecar" not in source, f"{runtime} never reads sidecars")
check("load_pages(file_id, name, upload.data, max_pages=settings.max_pages)"
      in (Path(__file__).parent / "service.py").read_text(encoding="utf-8"),
      "service.py gets pages only through page_source.load_pages, without a sidecar directory")

print(f"\nALL {passed} CHECKS PASSED")
