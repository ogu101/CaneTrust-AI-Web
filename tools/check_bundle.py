"""
Check what Vercel would deploy, without deploying.

    .venv/bin/python tools/check_bundle.py

1. Simulates vercel.json's functions["api/index.py"].excludeFiles over the tracked files
   (git ls-files) and lists what the function bundle would hold.
2. Confirms the runtime assets are in it (template, rules, defaults table, chain families) and
   that nothing that must stay out is (tests, fixtures, tools, docs, real documents, .env).
3. Copies ONLY the bundled files to a temporary directory, makes it read-only, and in a fresh
   interpreter with VERCEL=1 imports the app from there, calls /api/health and a refused
   /api/triage (a scan), and checks that: every project module imported came from the bundle
   (so nothing excluded is imported), and no file was written anywhere in the bundle.
4. Checks requirements.txt names a distribution for every third-party module the bundle imports.

No network, no model call. Exit status 1 on any problem.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = "api/index.py"

MUST_INCLUDE = ["api/index.py", "public/index.html", "requirements.txt", "assets/MiLA_Annotation_Template.xlsx",
                "assets/annotation_rules.md", "assets/triage_rules.md", "assets/default_treatments.json",
                "assets/chain_families.json"]
MUST_EXCLUDE_PREFIXES = ["fixtures/", "tools/", "docs/", "ui_test/", "test_", "testkit.py", "dev_stub_server.py",
                         "cli.py", "config.py", ".env", "INPUT/", "OUTPUT/", "private/", "scratchpad/", ".claude/"]
# Import name -> the requirements.txt distribution that provides it (directly or as a dependency).
DISTRIBUTIONS = {"anthropic": "anthropic", "fastapi": "fastapi", "starlette": "fastapi", "pydantic": "pydantic",
                 "openpyxl": "openpyxl", "pypdf": "pypdf", "dotenv": "python-dotenv", "multipart": "python-multipart",
                 "python_multipart": "python-multipart"}


def expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    return [p for option in m.group(1).split(",") for p in expand_braces(head + option + tail)]


def glob_to_regex(glob: str) -> re.Pattern:
    """minimatch-style: ** crosses directories, * and ? do not."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def bundle_files() -> tuple[list[str], list[str]]:
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    excludes = [glob_to_regex(g) for g in expand_braces(config["functions"][ENTRY]["excludeFiles"])]
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    files = sorted(f for f in tracked.decode().split("\0") if f and (ROOT / f).is_file())
    kept = [f for f in files if not any(rx.match(f) for rx in excludes)]
    return kept, files


PROBE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
os.environ["VERCEL"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
from fastapi.testclient import TestClient
import api.index as app_module
client = TestClient(app_module.app)
health = client.get("/api/health").json()
w = __import__("pypdf").PdfWriter(); w.add_blank_page(width=612, height=792)
import io; buf = io.BytesIO(); w.write(buf)
os.environ["CANETRUST_ALLOW_OPEN"] = "1"
scan = client.post("/api/triage", files=[("files", ("scan.pdf", buf.getvalue(), "application/pdf"))])
root = os.getcwd()
mods = {name: getattr(m, "__file__", None) for name, m in list(sys.modules.items())}
print(json.dumps({"health": health, "scan_status": scan.status_code, "scan_code": scan.json().get("code"),
                  "modules": {k: v for k, v in mods.items() if v}}))
"""


def main() -> int:
    problems: list[str] = []
    kept, tracked = bundle_files()
    print(f"{len(tracked)} tracked files; {len(kept)} in the function bundle:")
    for f in kept:
        print(f"  {f}")
    for f in MUST_INCLUDE:
        if f not in kept:
            problems.append(f"missing from the bundle: {f}")
    for f in kept:
        if any(f.startswith(p) for p in MUST_EXCLUDE_PREFIXES):
            problems.append(f"must not be deployed: {f}")

    # Import the app from a read-only copy of exactly the bundle.
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp).resolve() / "bundle"
        for f in kept:
            (bundle / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, bundle / f)
        before = {p: p.stat().st_mtime for p in bundle.rglob("*")}
        for p in [bundle, *bundle.rglob("*")]:
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CANETRUST_", "ANTHROPIC_"))}
        env.update(VERCEL="1", PYTHONDONTWRITEBYTECODE="1", CANETRUST_DISABLE_DOTENV="1")
        run = subprocess.run([sys.executable, "-c", PROBE], cwd=bundle, env=env, capture_output=True, text=True)
        after = {p: p.stat().st_mtime for p in bundle.rglob("*")}
        for p in [bundle, *bundle.rglob("*")]:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
        if run.returncode != 0:
            problems.append("the app did not import from the bundle:\n" + run.stderr[-3000:])
        else:
            report = json.loads(run.stdout.strip().splitlines()[-1])
            health = report["health"]
            print(f"\nImported with VERCEL=1 from the bundle: assets_present={health['assets_present']}, "
                  f"refused scan -> HTTP {report['scan_status']} {report['scan_code']}")
            if not health["assets_present"]:
                problems.append("/api/health reports assets missing in the bundle")
            if report["scan_status"] != 422:
                problems.append(f"a scan should be refused with 422, got {report['scan_status']}")
            stdlib = sysconfig.get_paths()["stdlib"]
            third_party, project = set(), set()
            for name, path in report["modules"].items():
                if path.startswith(str(bundle)):
                    project.add(name)
                elif str(ROOT) in path and ".venv" not in path:
                    problems.append(f"imported from outside the bundle (excluded file?): {name}")
                elif "site-packages" in path and "." not in name:
                    third_party.add(name)
            print(f"Project modules imported from the bundle: {', '.join(sorted(project))}")
            if not {"api.index", "service", "annotate", "workbook", "triage"} <= project:
                problems.append("the app's own modules were not imported from the bundle copy")
        written = sorted(str(p.relative_to(bundle)) for p in set(after) - set(before))
        changed = sorted(str(p.relative_to(bundle)) for p in before if p in after and after[p] != before[p])
        if written or changed:
            problems.append(f"files were written in the bundle: {written + changed}")
        else:
            print("Nothing was written to disk in the bundle.")

    # Every third-party module the bundle's own code imports must come from requirements.txt.
    required = {re.split(r"[<>=\[ ]", line.strip())[0].lower() for line in (ROOT / "requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")}
    stdlib_names = set(sys.stdlib_module_names)
    local = {Path(f).stem for f in kept if f.endswith(".py")} | {"api"}
    imported = set()
    for f in kept:
        if f.endswith(".py"):
            for node in ast.walk(ast.parse((ROOT / f).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])
    for name in sorted(imported - stdlib_names - local - {"__future__"}):
        dist = DISTRIBUTIONS.get(name)
        if dist is None:
            problems.append(f"no known distribution for imported module {name!r}; add it to requirements.txt and DISTRIBUTIONS")
        elif dist not in required:
            problems.append(f"{name!r} is imported but {dist} is not in requirements.txt")
    print(f"Third-party imports in the bundle's code: {', '.join(sorted(imported - stdlib_names - local - {'__future__'}))}")
    print(f"requirements.txt: {', '.join(sorted(required))}")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nBundle OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
