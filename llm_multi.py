"""
Single-call extraction for a packet (original trust + amendments).

Same shape as llm.py, with three differences:
  * the user message is the whole packet, labeled and in reading order;
  * the output has history for every field, so it is much larger. It is
    STREAMED, because the SDK refuses non-streaming requests whose max_tokens
    implies a runtime over ~10 minutes (roughly 21k tokens);
  * the result is reconciled against the packet, so every `amendments` list
    lines up with the amendments actually supplied.

Caching is the same: system prompt and tool schema are identical on every
packet, so they are cached; the packet text is not.
"""

import json
from pathlib import Path

import anthropic

from packet import Packet
from schema_multi import PacketAnnotation, reconcile_with_packet


def render_packet(packet: Packet) -> str:
    """The whole packet, labeled, in the order the model must read it."""
    lines = [f"Document: {packet.name}", "", "Contents of this packet, in reading order:"]
    lines.append(f"  - Original trust: {packet.trust.label}")
    for a in packet.amendments:
        lines.append(f"  - Amendment {a.number}: {a.label}")

    lines += ["", f"Amendment numbers to report on, exactly: {packet.amendment_numbers}"]
    if packet.missing_numbers:
        lines.append(
            f"NOT PROVIDED: amendment(s) {packet.missing_numbers}. The chain has gaps; "
            f"treat operative answers accordingly."
        )

    lines += ["", f"=== ORIGINAL TRUST — {packet.trust.label} ===", packet.trust.text]
    for a in packet.amendments:
        lines += ["", f"=== AMENDMENT {a.number} — {a.label} ===", a.text]
    return "\n".join(lines)


def build_packet_request(
    packet: Packet,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    rules_path: Path,
) -> dict:
    return dict(
        model=model,
        max_tokens=max_tokens,
        # Adaptive thinking is the mode that still allows the forced tool_choice below.
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=[
            {
                "type": "text",
                "text": rules_path.read_text(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {
                "name": "analyze_documents",
                "description": (
                    "Record the complete structured extraction for this packet. "
                    "Call this exactly once, after reading the original trust and "
                    "every amendment in order."
                ),
                "input_schema": PacketAnnotation.model_json_schema(),
                "strict": True,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tool_choice={"type": "tool", "name": "analyze_documents"},
        messages=[{"role": "user", "content": render_packet(packet)}],
    )


def analyze_packet(
    packet: Packet,
    *,
    api_key: str,
    model: str,
    effort: str,
    max_tokens: int,
    rules_path: Path,
    debug_dir: Path | None = None,
) -> PacketAnnotation:
    client = anthropic.Anthropic(api_key=api_key)
    request = build_packet_request(
        packet, model=model, effort=effort, max_tokens=max_tokens, rules_path=rules_path
    )

    with client.messages.stream(**request) as stream:
        response = stream.get_final_message()

    tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
    if not tool_use_blocks:
        raise RuntimeError(
            f"No tool_use block returned for {packet.name!r} "
            f"(stop_reason={response.stop_reason}): {response.content}"
        )
    raw = tool_use_blocks[0].input

    # Keep what the model actually returned, before any validation, when a debug
    # directory is given (a deployed filesystem is read-only apart from /tmp).
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "last_raw_output_packet.json"
        debug_path.write_text(json.dumps(raw, indent=2))
        print(f"[debug] wrote raw tool output to {debug_path}")

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Output was cut off at max_tokens={max_tokens} for {packet.name!r}, so the "
            f"result is incomplete. Raise MAX_TOKENS_PER_REQUEST_MULTI in config.py "
            f"(thinking tokens share this budget)."
        )

    annotation = PacketAnnotation.model_validate(raw)

    warnings = reconcile_with_packet(annotation, packet.amendment_numbers)
    for w in warnings[:10]:
        print(f"[warning] {w}")
    if len(warnings) > 10:
        print(f"[warning] ...and {len(warnings) - 10} more")

    usage = response.usage
    print(
        f"[{packet.name}] input={usage.input_tokens} "
        f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
        f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
        f"output={usage.output_tokens}"
    )
    return annotation
