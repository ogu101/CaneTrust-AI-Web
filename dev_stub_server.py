"""
Run the whole web app locally with a FAKE model: no API key, no cost, canned answers.

    python dev_stub_server.py                 # starts the server and opens your browser
    python dev_stub_server.py --no-browser    # start it without opening a browser
    PORT=9000 python dev_stub_server.py       # use another port

This is a SERVER. It does not exit; it keeps running and waits for requests, so a quiet
terminal after the banner is normal. Stop it with Ctrl+C.

Use it to try the upload page, the progress messages and the workbook download. The workbooks
it produces contain placeholder text, not real analysis. For the real thing, put
ANTHROPIC_API_KEY in .env and run `uvicorn api.index:app`.
"""

import os
import socket
import sys
import threading
import webbrowser

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-local-ui-testing")

import uvicorn

import service
import testkit
from api.index import app

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8000"))
PAUSE_SECONDS = float(os.getenv("STUB_PAUSE_SECONDS", "1.5"))

# Every model call (triage, facts, annotation) is answered by testkit.StubModel: one document per
# file, typed from its file name and dated from its first date; placeholder annotations.
testkit.install_stub(service, testkit.StubModel(pause=PAUSE_SECONDS))


def port_in_use(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"

    if port_in_use(HOST, PORT):
        sys.exit(
            f"\nPort {PORT} is already in use, so a server may already be running there.\n"
            f"Open {url} to check, stop the other one (Ctrl+C in its terminal), or use a "
            f"different port:  PORT=9000 python dev_stub_server.py\n"
        )

    print()
    print("CaneTrust dev server  (FAKE model: no API key, no cost, placeholder results)")
    print(f"  Open:  {url}")
    print("  Stop:  press Ctrl+C")
    print("  This terminal stays quiet until you use the page; that is normal.")
    print()

    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
