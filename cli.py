"""
Usage:
    python cli.py

(This was main.py. Vercel treats a file named main.py as a candidate entrypoint for the web
app, so the command-line tool lives here to keep the two unambiguous.)

Inputs come from config.py; model and key settings from the environment (.env). The matter is
an original trust plus a possibly empty list of AMENDMENTS. It is routed by the same
service.annotate_packet() that the web app uses, so the two can't behave differently.
"""

from pathlib import Path

from config import AMENDMENTS, DOC_NAME, LAYER_LABEL, OUTPUT_DIR, RESOURCES_FILE
from packet import build_packet
from service import annotate_packet
from settings import load_local_env, load_settings


def main() -> None:
    load_local_env()
    settings = load_settings()

    # Extraction happens here, inside the pipeline: PDFs in config.py are read directly.
    packet = build_packet(DOC_NAME, LAYER_LABEL, RESOURCES_FILE, AMENDMENTS)
    for warning in packet.warnings:
        print(f"[warning] {warning}")

    result = annotate_packet(packet, settings, progress=lambda message: print(f"[progress] {message}"))

    out_path = Path(OUTPUT_DIR) / result.filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(result.xlsx)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
