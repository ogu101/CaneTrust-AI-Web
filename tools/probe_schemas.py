"""
Ask the real API whether each tool schema is accepted (a paid call, but a tiny one).

    .venv/bin/python tools/probe_schemas.py [--only NAME ...]

One minimal request per tool: the real tool definition (as the pipeline sends it), the
model the pipeline uses, a one-line user message with no document text, max_tokens 64 and no
thinking. A strict schema the API cannot compile is refused with a 400 before any output, so
"accepted" means the schema compiled. Prints accepted or rejected, the HTTP status, the API's
error message, our own optional/union parameter counts, and the token usage of every call.

The model the pipeline uses is CANETRUST_MODEL / CANETRUST_TRIAGE_MODEL (settings.py). Exit
status 1 if any schema the pipeline actually sends is rejected (a rejected strict trial only
reports that the tool must stay non-strict).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from schema_limits import strict_schema_counts, strict_schema_problems  # noqa: E402
from settings import load_local_env, load_settings, require_api_key  # noqa: E402

PROBE_MESSAGE = "Schema probe. No document is attached. Call the tool with placeholder values."


def probes(settings) -> list[tuple[str, str, dict]]:
    """(name, model, tool definition) for every tool the pipeline sends, exactly as sent, plus a
    strict trial of any tool sent without strict (to see whether it could be made strict)."""
    from annotate import tool_definition as annotation_tool
    from facts import tool_definition as facts_tool
    from triage_schema import tool_definition as triage_tool
    out = [("triage", settings.triage_model, triage_tool()),
           ("facts", settings.triage_model, facts_tool()),
           ("annotation", settings.model, annotation_tool())]
    for name, model, tool in list(out):
        if not tool.get("strict"):
            out.append((f"{name} (strict trial)", model, {**tool, "strict": True}))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="*", help="probe only these names")
    args = parser.parse_args()

    load_local_env()
    settings = load_settings()
    client = anthropic.Anthropic(api_key=require_api_key(settings))

    rejected = 0
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    for name, model, tool in probes(settings):
        if args.only and name not in args.only:
            continue
        tool = {k: v for k, v in tool.items() if k != "cache_control"}
        counts = strict_schema_counts(tool["input_schema"])
        local = strict_schema_problems(tool["input_schema"])
        print(f"== {name}  (model {model}, tool {tool['name']})")
        print(f"   our counts: optional={counts['optional']} union={counts['union']}; "
              f"local problems: {len(local)}")
        for p in local:
            print(f"     - {p}")
        try:
            response = client.messages.create(
                model=model, max_tokens=64, tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": PROBE_MESSAGE}])
        except anthropic.APIStatusError as exc:
            rejected += 0 if name.endswith("(strict trial)") else 1
            totals["calls"] += 1
            message = getattr(exc, "body", None) or {}
            if isinstance(message, dict):
                message = (message.get("error") or {}).get("message", str(exc))
            print(f"   REJECTED  status {exc.status_code}: {message}")
            continue
        usage = response.usage
        totals["calls"] += 1
        totals["input_tokens"] += usage.input_tokens
        totals["output_tokens"] += usage.output_tokens
        print(f"   accepted  status 200, stop_reason {response.stop_reason}, "
              f"input {usage.input_tokens} tokens, output {usage.output_tokens} tokens")
    print(f"\n{totals['calls']} call(s); input {totals['input_tokens']} tokens, "
          f"output {totals['output_tokens']} tokens; {rejected} rejected (strict trials not counted)")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
