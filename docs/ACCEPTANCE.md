# Acceptance on the real fixtures

The plan is to build the whole pipeline first, run the real documents in `fixtures/gay/` through it at the end (milestone 9, once OCR exists), and debug backwards from the final workbook. This file is the ladder for that debugging.

## Before you start

- The expected files (`expected_map.json`, `expected_facts.json`, `expected_findings.json`) were written by hand from page images. A failure can mean the expectation is wrong, not the code. Check the page image before changing code. Correct an expected file only when the image shows it is wrong, never just to make a check pass, and say so in the commit.
- The manual review workbook (`fixtures/gay/ground_truth/MiLA_Annotation_Gay_Manual_Review.xlsx`) is a reference, not ground truth. Its caveats are listed in `expected_map.json` under `ground_truth_workbook`.
- Before OCR exists, rungs 5 and 6 can still be run by placing a sidecar `fixtures/gay/<filename>.pages.json` (a JSON list of page texts, one per sheet, in sheet order; see `page_source.py`).

## Rule: fix at the lowest failing rung first

Run every rung and find the lowest one that fails (the highest number below). Fix that first, then rerun from the top. A wrong page label or a wrong document boundary breaks every rung above it, so fixing the workbook first only hides the real fault.

## The ladder

Rungs are listed in the order they are checked, from the final output back toward the input.

### 1. Workbook

- **Compared:** the generated workbook against `ground_truth/MiLA_Annotation_Gay_Manual_Review.xlsx`, sheet by sheet (trust chain, will chain, accounting), following the sheet map and caveats in `expected_map.json` `ground_truth_workbook`. Also the must-match values in `expected_findings.json`: two University mentions in the trust chain with their printed pages, the University as primary residuary beneficiary, the successor trustees, no gift to the University in the will chain, the codicil's two changes, and the accounting not annotated but listed in the packet map as log only.
- **To blame if it fails:** the unified schema, the annotation prompt and rules, the renderer and cell map (milestone 6). Also the packet map sheet.
- **Command:** to be added (milestone 9), e.g. `.venv/bin/python acceptance_gay.py workbook`.

### 2. Facts

- **Compared:** facts-lane output against `expected_facts.json`: date of death, probate case, personal representative, successor trustee, estate attorney, each with its sources. This includes the planted address conflict for Mahalie Ann Fulton: both addresses are shown, and the 2025 accounting wins as the newest source. Also that no personal phone or email is found for the representative.
- **To blame if it fails:** the facts lane (milestone 5). Also check whether the triage dates (rung 4) gave it the wrong "newest" source.
- **Command:** to be added (milestone 9), e.g. `.venv/bin/python acceptance_gay.py facts`.

### 3. Chains

- **Compared:** chains against `expected_map.json` `chains`. The trust chain is the 2022 restatement alone, with the 2021 trust and the 2022-06-09 amendment superseded, not gaps. The will chain is the will plus the codicil. Exactly two chains.
- **To blame if it fails:** chain building (milestone 4): date ordering, restatement reset, codicil-as-amendment, gap rules. Also check that triage gave each document the right type and date.
- **Command:** to be added (milestone 9), e.g. `.venv/bin/python acceptance_gay.py chains`.

### 4. Triage

- **Compared:** the triage document map against `expected_map.json` `documents`. **Check boundaries first** (file, first and last sheet for each document), then type, date, `refers_to`, `amends` and treatment, then `expected_counts`. Note that the accounting is in a file named "Amendment": content must beat the file name. The nine documents in the will packet are in non-chronological order.
- **To blame if it fails:** triage (milestone 3): page previews, computed features, the triage prompt, the defaults table, range validation.
- **Command:** `.venv/bin/python tools/live_triage.py fixtures/gay/*.pdf --expected fixtures/gay/expected_map.json` (a paid model call; needs a text layer or sidecars, and `ANTHROPIC_API_KEY`). It prints the map, then a mismatch table with boundaries first, and exits 1 on a mismatch. `CANETRUST_LIVE=1 .venv/bin/python test_gay_fixtures.py` runs the same comparison as part of that script.

### 5. Page labels

- **Compared:** for each document, `label_document()` over its sheet range against `label_rules` in `expected_map.json` (labels and sources; rules with `"assert": false` are skipped).
- **To blame if it fails:** `page_labels.py` (candidate patterns, offset choice, front matter, unnumbered convention). If the text from rung 6 is wrong, blame that instead.
- **Command:** `.venv/bin/python test_gay_fixtures.py`. It prints a mismatch table. Its text checks skip until a text layer or sidecar exists.

### 6. Page text

- **Compared:** spot check by eye. For a few sheets per document (a first page, a numbered body page, a footer-heavy page, a form), compare the page text from `page_source.load_pages()` with the page image. Check: one text per sheet in sheet order, the headers and footers that carry printed numbers are present, no pages swapped or merged.
- **To blame if it fails:** the OCR step (milestone 9) or the sidecar that stands in for it, then `page_source.py`.
- **Command:** to be added (milestone 9), e.g. `.venv/bin/python acceptance_gay.py text --sheets 1,7,47`. Print to the terminal only; never log page text.
