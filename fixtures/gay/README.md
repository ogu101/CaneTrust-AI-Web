# fixtures/gay

Real documents provided by the team, cleared for use as test material. They are scanned images with no text layer, so the pipeline cannot read them until the OCR step exists. They are used for end-to-end acceptance at the end of the build (see `docs/ACCEPTANCE.md`), not for the unit tests.

Files to place here:

- `Gay__Lawrence__Revocable_Trust.pdf` (48 sheets): one amended and restated trust.
- `Gay__Lawrence__Revocable_Trust_Amendment_10-25-21.pdf` (50 sheets): despite its name, a trust accounting. Keep the file name as is; it tests that content beats file names.
- `Gay__Lawrence__Last_Will_and_Codicil_and_Notice_of_Admin_SIGNED.pdf` (21 sheets): a packet of nine documents in non-chronological order.
- `ground_truth/MiLA_Annotation_Gay_Manual_Review.xlsx`: the manual review, one sheet per file. A reference, not ground truth.

Written by hand from the page images:

- `expected_map.json`: documents, page ranges, types, dates, treatments, chains, and the expected printed-page label of every sheet.
- `expected_facts.json`: what the facts lane should find, including one planted address conflict.
- `expected_findings.json`: a few high-signal results the final workbook must contain.

These expected files were written by reading images, so when a check fails, suspect the map as well as the code. Do not add other real documents here.
