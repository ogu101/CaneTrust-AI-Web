"""Save a workbook to a path or to an in-memory buffer.

A serverless function has no durable disk to write a result to, so the renderers accept
either a path (command-line use) or any object with .write (an io.BytesIO, for the web app).
"""

import os
from pathlib import Path


def save_workbook(workbook, out) -> None:
    if hasattr(out, "write"):
        workbook.save(out)
        return
    path = Path(os.fspath(out))
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
