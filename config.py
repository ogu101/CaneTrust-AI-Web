# CaneTrust-AI: inputs for the COMMAND-LINE tool (cli.py).
#
# The web app does not read this file; users upload their documents instead. Model, token
# limits, API key and asset paths live in settings.py and come from environment variables
# (a local .env file, or the project's environment variables on Vercel).

# ============================================================================
# THE MATTER
# ============================================================================

DOC_NAME = "Marlowe Family Living Trust"

# The ORIGINAL trust. A text-based PDF or a .txt file: PDFs are extracted automatically.
RESOURCES_FILE = "INPUT/marlowe/00_marlowe_trust_original.pdf"
LAYER_LABEL = "Marlowe Family Living Trust (1998)"

# Amendments to that trust. Triage uses this list:
#   []        -> no amendments: single-document pipeline, v0 template
#   non-empty -> multi-document pipeline, amendments template
# "number" is the amendment's order (1 = First Amendment); list order does not matter.
# Each "path" is a text-based PDF or a .txt file. Up to 8 amendments.
AMENDMENTS = [
    # {"number": 1, "label": "First Amendment (2004)",  "path": "INPUT/marlowe/01_marlowe_first_amendment.pdf"},
    # {"number": 2, "label": "Second Amendment (2009)", "path": "INPUT/marlowe/02_marlowe_second_amendment.pdf"},
]

# ============================================================================
# OUTPUT
# ============================================================================

OUTPUT_DIR = "OUTPUT"
