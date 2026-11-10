# CaneTrust-AI: inputs for the COMMAND-LINE tool (cli.py).
#
# The web app does not read this file; users upload their documents instead. Model, token
# limits, API key and asset paths live in settings.py and come from environment variables
# (a local .env file, or the project's environment variables on Vercel).

# ============================================================================
# THE MATTER
# ============================================================================

# Every file for one matter, in any order: a trust, its amendments, a will, codicils, notices,
# letters, accountings, or one PDF packet holding all of them. Text-based PDFs or .txt files.
# File names are not trusted: triage classifies each document by its content, and chains are
# ordered by the dates in the documents, never by this list.
FILES = [
    "fixtures/00_marlowe_trust_original.pdf",
    "fixtures/01_marlowe_first_amendment.pdf",
    "fixtures/02_marlowe_second_amendment.pdf",
    "fixtures/03_marlowe_third_amendment_and_restatement.pdf",
    "fixtures/04_marlowe_fourth_amendment.pdf",
]

# The matter's name, used for the workbook's file name. Empty: the first file's name.
DOC_NAME = "Marlowe Family Living Trust"

# ============================================================================
# OUTPUT
# ============================================================================

OUTPUT_DIR = "OUTPUT"
