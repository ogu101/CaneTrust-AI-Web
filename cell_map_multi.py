"""
Column anchors for MiLA_Annotation_Template_Amendments_v0.xlsx, sheet "Annotation".

Rows are identical to the single-document template (no rows were inserted when
the amendment columns were added), so FIXED_ROWS, VARIABLE_SECTIONS and HEADER
come straight from cell_map.py. This file only adds the columns:

    A            item label
    B-E          ORIGINAL TRUST   answer / where / status / note
    F            gutter
    G-N          AMENDMENTS       one column per amendment (8 slots)
    O            spacer
    P-S          OPERATIVE        answer / where (controlling provision) / status / note
    T            gutter
"""

from cell_map import HEADER, FIXED_ROWS, VARIABLE_SECTIONS, STATUS_TO_CELL  # noqa: F401  (re-exported)

ORIGINAL_COLS = dict(answer="B", citation="C", status="D", note="E")
OPERATIVE_COLS = dict(answer="P", citation="Q", status="R", note="S")

AMENDMENT_FIRST_COL = 7          # G
AMENDMENT_SLOTS = 8              # G..N
LAST_COL = 20                    # T: styles are copied A..T when rows are inserted

AMENDMENT_HEADER_ROW = 11

FORM_TO_TEXT = {
    "changes_only": "changes only",
    "restating": "full restatement",
    "unclear": "form unclear",
}

# Rows of the template that take Status dropdowns / per-amendment entries
# BEFORE any rows are inserted: sections 1-3 are fixed; 4 and 5 are variable.
FIXED_INPUT_RANGES = [(13, 19), (21, 23), (25, 27)]
