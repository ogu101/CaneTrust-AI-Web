"""
Usage:
    python cli.py

(This was main.py. Vercel treats a file named main.py as a candidate entrypoint for the web
app, so the command-line tool lives here to keep the two unambiguous.)

Inputs come from config.py; model and key settings from the environment (.env). The files go
through the same service functions the web app uses (load_files -> triage_packet ->
annotate_plan), with triage's document map accepted as it stands (auto-confirm). If triage
needs a person's decision, the run stops and says so: use the web app's confirm screen.

Prints the document map (ids, files, sheets, types, dates, treatments: never document text)
and writes the workbook to OUTPUT_DIR.
"""

import sys
from pathlib import Path

from config import DOC_NAME, FILES, OUTPUT_DIR
from service import UploadedFile, annotate_plan, auto_confirm_problem, load_files, triage_packet
from settings import load_local_env, load_settings, require_api_key


def read_files(paths: list[str]) -> list[UploadedFile]:
    files = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"file not found: {path}")
        files.append(UploadedFile(p.name, p.read_bytes()))
    return files


def main() -> None:
    load_local_env()
    settings = load_settings()
    require_api_key(settings)
    progress = lambda message: print(f"[progress] {message}")      # noqa: E731

    loaded = load_files(read_files(FILES), settings)
    outcome = triage_packet(loaded, settings, progress, name=DOC_NAME or None)
    for warning in outcome.warnings:
        print(f"[warning] {warning}")
    for d in outcome.plan.documents:
        print(f"[document] {d.id}  {d.file_id} sheets {d.first_sheet}-{d.last_sheet}  {d.doc_type}  "
              f"{d.date or 'undated'}  {d.treatment}")
    problem = auto_confirm_problem(outcome.plan)
    if problem:
        sys.exit(f"[stopped] {problem}")

    result = annotate_plan(outcome.plan, loaded.pages_by_file, settings, progress)
    for warning in result.warnings:
        print(f"[warning] {warning}")
    out_path = Path(OUTPUT_DIR) / result.filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(result.xlsx)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
