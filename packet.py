"""
Input model and triage.

A "packet" is everything submitted for one matter: the original trust plus zero or more
amendments. Triage looks only at what the user supplied, never at what a model thinks the
documents are, so routing is deterministic and free:

    no amendments supplied  -> "single"  (v0 template)
    any amendment supplied  -> "multi"   (amendments template)

Two ways in, one result:
    make_packet(...)   from text already in memory (the web app: uploads -> extract -> here)
    build_packet(...)  from file paths on disk (the command-line tool); PDFs and .txt both work
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from extract import ExtractionError, extract_text

# Number of amendment columns in MiLA_Annotation_Template_Amendments_v0.xlsx.
MAX_AMENDMENTS = 8


@dataclass
class Instrument:
    number: int          # 0 = original trust, 1 = First Amendment, 2 = Second, ...
    label: str           # e.g. "Second Amendment (2004)"
    text: str


@dataclass
class Packet:
    name: str
    trust: Instrument
    amendments: list[Instrument] = field(default_factory=list)   # ascending by number
    warnings: list[str] = field(default_factory=list)            # e.g. pages with no text

    @property
    def amendment_numbers(self) -> list[int]:
        return [a.number for a in self.amendments]

    @property
    def missing_numbers(self) -> list[int]:
        """Numbers below the highest supplied amendment that were not supplied.
        If Amendment 4 is present but 3 is not, the chain is incomplete and
        operative answers can only be provisional."""
        if not self.amendments:
            return []
        have = set(self.amendment_numbers)
        return [n for n in range(1, max(have) + 1) if n not in have]

    @property
    def total_chars(self) -> int:
        return len(self.trust.text) + sum(len(a.text) for a in self.amendments)


def make_packet(
    name: str,
    trust_label: str,
    trust_text: str,
    amendments: list[dict],
    *,
    warnings: list[str] | None = None,
    max_chars: int | None = None,
) -> Packet:
    """amendments: [{"number": 1, "label": "First Amendment (2003)", "text": "..."}, ...]
    Order in the list does not matter; amendments are sorted by number."""
    if not trust_text.strip():
        raise ValueError("Original trust: the text is empty")

    seen: set[int] = set()
    instruments: list[Instrument] = []
    for item in amendments:
        missing_keys = {"number", "label", "text"} - item.keys()
        if missing_keys:
            raise ValueError(f"Amendment entry is missing: {sorted(missing_keys)}")
        number = item["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError(f"Amendment number must be a whole number >= 1, got {number!r}")
        if number in seen:
            raise ValueError(f"Amendment number {number} appears more than once")
        seen.add(number)
        if not item["text"].strip():
            raise ValueError(f"Amendment {number}: the text is empty")
        instruments.append(Instrument(number, item["label"], item["text"]))

    if len(instruments) > MAX_AMENDMENTS:
        raise ValueError(
            f"{len(instruments)} amendments supplied, but the amendments template has "
            f"room for {MAX_AMENDMENTS}."
        )

    instruments.sort(key=lambda a: a.number)
    packet = Packet(
        name=name,
        trust=Instrument(0, trust_label, trust_text),
        amendments=instruments,
        warnings=list(warnings or []),
    )
    if max_chars is not None and packet.total_chars > max_chars:
        raise ValueError(
            f"The documents total {packet.total_chars:,} characters, over the "
            f"{max_chars:,}-character limit for one packet."
        )
    return packet


def _read_path(path: str, what: str) -> tuple[str, list[str]]:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{what}: file not found: {path}")
    try:
        extracted = extract_text(p.name, p.read_bytes())
    except ExtractionError as exc:
        raise ValueError(f"{what}: {exc}") from exc
    return extracted.text, extracted.warnings


def build_packet(
    name: str,
    trust_label: str,
    trust_path: str,
    amendment_specs: list[dict],
) -> Packet:
    """Command-line entry: amendment_specs = [{"number", "label", "path"}, ...].
    Each path may be a text-based PDF or a .txt file; PDFs are extracted here."""
    trust_text, warnings = _read_path(trust_path, "Original trust")

    items = []
    for spec in amendment_specs:
        missing_keys = {"number", "label", "path"} - spec.keys()
        if missing_keys:
            raise ValueError(f"Amendment entry {spec!r} is missing: {sorted(missing_keys)}")
        text, spec_warnings = _read_path(spec["path"], f"Amendment {spec['number']}")
        warnings += spec_warnings
        items.append({"number": spec["number"], "label": spec["label"], "text": text})

    return make_packet(name, trust_label, trust_text, items, warnings=warnings)


def triage(packet: Packet) -> Literal["single", "multi"]:
    return "multi" if packet.amendments else "single"
