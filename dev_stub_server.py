"""
Run the web app locally with a FAKE model: no API key, no cost, canned answers.

    python dev_stub_server.py            # http://127.0.0.1:8000
    PORT=9000 python dev_stub_server.py

Use it to try the upload page, the streamed progress and the workbook download. The
workbooks it produces contain placeholder text, not real analysis. For the real thing run
`uvicorn api.index:app` with ANTHROPIC_API_KEY set, or `vercel dev`.
"""

import os
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-local-ui-testing")

import uvicorn

import service
from api.index import app
from schema_multi import reconcile_with_packet
from testkit import make_packet_annotation, tiny_single_annotation

PAUSE_SECONDS = float(os.getenv("STUB_PAUSE_SECONDS", "1.5"))


def fake_single(name, layers, **kwargs):
    time.sleep(PAUSE_SECONDS)
    return tiny_single_annotation()


def fake_multi(packet, **kwargs):
    time.sleep(PAUSE_SECONDS)
    annotation = make_packet_annotation(n_terms=2, n_mentions=2)
    reconcile_with_packet(annotation, packet.amendment_numbers)
    return annotation


service.analyze_document = fake_single
service.analyze_packet = fake_multi

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")), log_level="warning")
