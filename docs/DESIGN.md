# Unified pipeline design

Status: agreed design, not yet built. Replaces the separate single-document and multi-document pipelines.

## Goal

One pipeline that accepts any mix of files and produces one Excel workbook for attorney review:

- one PDF packet holding a trust, amendments, a will, codicils, notices, letters, court forms, and accountings;
- separate files for the trust and each amendment;
- a single trust PDF.

The input is always a bag of pages. Branching happens on what is found in the data (how many amendments, what kinds of documents), never on how many files were uploaded.

## Decisions already made

1. One pipeline. The old `triage()` by amendment count goes away.
2. No file roles required from the user. File names are not trusted (a file named "amendment" can be an accounting). Classify by content.
3. OCR is deferred to the end of the build. Until then the pipeline reads PDF text layers only. Page text comes through one seam ("Page source", below), so the OCR step plugs in later without changing anything downstream.
4. Citations use the page numbers printed on the physical pages, never PDF page positions.
5. Document handling is flexible: each document gets a treatment (annotate, facts only, log only). Triage proposes it from an editable defaults table, and a person can change it on the confirm screen.
6. Facts (contacts, dates, who is serving) can come from any document. Dispositive fields (the gift, income beneficiaries, restrictions) come only from the instruments themselves.
7. A confirm step sits between triage and annotation, skipped only when the packet is trivial and triage is confident.
8. Vercel: triage and annotation are two separate requests. Never hold a request open while a person decides.

## Page reader boundary

Input: file bytes. Output: a list of page records.

```
PageRecord: file_id, position (1-based sheet in the file), text,
            label_candidate (raw printed number if found),
            label, label_source (printed | inferred | unnumbered)
```

Today the text comes from the PDF text layer. A later OCR step fills the same records.

## Page source

`page_source.load_pages(file_id, filename, data)` is the only way the pipeline gets pages. It returns page records and tries these in order:

1. The PDF text layer (or a .txt file), through the page reader.
2. Tests and dev tools only: a sidecar file `<filename>.pages.json` in a directory the caller names. It is read only when the file has no text layer.
3. Otherwise the file is refused with the existing `no_text_layer` error.

The web API never passes a sidecar directory, so it never reads sidecars.

The sidecar format is the plug-in point for OCR. Whatever the OCR step produces must be a JSON list of page texts in sheet order, exactly one string per sheet of the PDF (`""` for a blank sheet). A sidecar with the wrong number of entries is refused. When OCR exists it becomes step 2 in the API too, and sidecars stay a test tool.

## Test material

- Milestones 1 to 7 test with tiny invented page strings, the fictional Marlowe PDFs, and stubbed model calls. No real documents.
- `fixtures/gay/` holds real scanned documents, cleared for use as fixtures, with expected results written by hand from the page images (`expected_map.json`, `expected_facts.json`, `expected_findings.json`). They have no text layer, so the pipeline cannot read them until OCR exists. They are the end-to-end acceptance test at the end of the build: run them through the whole pipeline and debug backwards from the final workbook, following `docs/ACCEPTANCE.md`.
- `test_gay_fixtures.py` checks that the expected files are consistent with the PDFs and with themselves. Its page-label checks skip until a text layer or sidecar exists.

## Printed page labels

Labels are computed per document, after triage finds the boundaries (numbering restarts in each document).

- Candidates come from footer and header patterns: `7`, `- 7 -`, `Page 7`, `Page 7 of 41`, roman numerals, form-style `1 of 2`.
- Within a document the sequence must climb by one. Fill gaps. If page 1 prints nothing and page 2 prints "2", infer 1.
- Front matter in roman numerals is separate from the arabic run.
- A document with no printed numbers is labeled `unnumbered, sheet N of M`. The exact convention is still to be confirmed with the team.
- The model sees markers like `[Page 7]` in the text and cites only those. Code checks that every cited label exists in that document.
- Remove the existing `[PDF page N of M]` markers and the rules text that tells the model to cite PDF pages.

## Triage

One model call over short previews of every page: opening lines, closing lines, footer, and features computed in code.

Computed in code, not by the model: printed page candidates, document-system numbers in footers, form numbers, dates found by pattern, a count of University mentions, coverage checks, the defaults table.

The model decides: where each document starts and ends, type, title, date, which instruments it refers to, whether it appears to contain a gift to the University, and a proposed treatment with a reason.

Output per document:

```
Document: id, file_id, first_page, last_page, type, title, date,
          refers_to[], revokes[], gift_to_university (yes | no | unclear),
          treatment, treatment_reason, confidence, evidence[]
```

Rules given to the model: a document may not span two files unless the file is itself a packet; every page belongs to exactly one document.

Validation in code: ranges cover every page exactly once, no overlaps, dates parse. On failure retry once with the problem stated, then send the result to the confirm screen marked as needing attention. Packets too large for one call use overlapping windows and stitch the boundary documents.

## Default treatments

| Treatment | Default for |
|---|---|
| Annotate | Trusts, restatements, amendments, wills, codicils |
| Facts only | Notices, letters, court forms, receipts |
| Log only | Accountings, blank forms, duplicates |

The table is editable configuration. Any non-annotated document that appears to contain a gift to the University is flagged.

## Chains

- Order by the date found in the text, never by file order, file name, or upload order.
- A restatement resets the chain. Earlier instruments are superseded, not missing.
- A codicil amends a will the way an amendment amends a trust. Same machinery.
- A referenced instrument that is not provided is a gap only if it came after the last restatement.
- Several chains per matter are normal (for example a trust chain and a will chain).
- Built by `chains.build_chains()` (pure logic) from `assets/chain_families.json`. Only annotated, non-excluded documents are chained; groups are family plus normalized principal; a user override "amends" wins.
- A document with no date is flagged and not chained. Annotated documents that cannot be chained (no date, `other_dispositive`) are annotated on their own as single-instrument sheets, flagged.
- Same-date documents keep file order and are flagged. With two originals, the latest is the base (flagged).
- Triage records `revokes[]` (what, date). A revoked instrument stays in the chain marked revoked. If a revoked instrument is a restatement, what it superseded becomes active again, flagged `revival_assumed`: an assumption for a person to confirm, not a ruling.
- A referenced amendment or codicil is a gap only if its date is known, it is after the base restatement (or there is none), and no provided document of that family has that date. References without a usable date are flagged, never gaps.

## Facts lane

Fields: personal representative and trustee names and contact details, dates of death, case numbers, attorney contacts. Any document may supply them. Each fact keeps its source document and date. The newest source wins and conflicts are shown, not hidden. Facts never feed the bequest, income, or restriction fields.

## Confirm screen

Shows files, then documents in file order. Each row: pages, type, date, treatment dropdown, note. Items needing a decision come first. A summary line counts documents by treatment.

A person can change type, date, treatment, and chain; split or merge page ranges; mark a document superseded; exclude a document. If triage suspects two matters in one packet it asks before continuing.

On confirm: recompute page labels if boundaries moved, rebuild chains, run annotation. Save the corrections with the job as an audit record.

## Workbook

One workbook per matter: a sheet per annotated chain plus a "Packet map" sheet listing every document with pages, type, date, treatment, reason, and flags. A chain with no amendments hides the amendment and final-position columns. One template replaces the two current ones.

## API and hosting (Vercel)

- `POST /api/triage`: files in, plan JSON and page texts out (NDJSON progress stream).
- `POST /api/annotate`: confirmed plan and page texts in, workbook out (NDJSON stream, workbook in the last event).
- State between the two calls is held by the browser for now. Move to a private Blob store with a deletion rule when packets or scans outgrow the request limits.
- Checked against Vercel docs in November 2026: request and response bodies are limited to 4.5 MB; function duration defaults to 300 s (Hobby maximum 300 s, Pro maximum 800 s); private Blob storage is generally available; direct browser uploads and signed URLs are JavaScript SDK features, not Python; Workflow hooks can pause for a person but Python support is beta.
- Never log document text.

## Milestones

Each milestone ends with tests passing and a commit.

| # | Milestone | Done when |
|---|---|---|
| 1 | Page reader and page labeler | Unit tests cover printed, inferred, and unnumbered labels, roman front matter, and per-document restarts, using synthetic PDFs |
| 2 | Page source seam and real-fixture harness | `page_source.load_pages()` exists with sidecar support for tests; `fixtures/gay/` expected files pass structure checks; `docs/ACCEPTANCE.md` written |
| 3 | Triage | With the model call stubbed, invented page strings produce the expected document map; validation failures are handled |
| 4 | Chains | Pure-logic tests for date ordering, restatement reset, codicil-as-amendment, and gap rules |
| 5 | Facts lane | Contacts come from notices and letters, with source and date, and conflicts are shown |
| 6 | Unified schema and workbook | Old single and multi tests pass through the unified path, then the old modules are removed; packet map sheet exists |
| 7 | Two-step API and confirm screen | API and UI tests pass; the confirm screen matches this document |
| 8 | Preview deploy and measurement | Real timings for triage and annotation recorded |
| 9 | OCR and end-to-end acceptance | OCR fills page records through the page source; `fixtures/gay/` passes every rung of `docs/ACCEPTANCE.md` |

## Out of scope for now

OCR for scanned documents, until milestone 9. Confidentiality routing beyond keeping documents out of logs and the repo.

## Open questions for the team

- Are wills annotated by default? The current assumption is yes, because they can gift to the University directly.
- The convention for citing pages in documents with no printed numbers.
- Whether another vendor may receive documents once OCR is added.
