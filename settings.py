"""
Settings shared by the command-line tool and the web app.

Everything comes from environment variables with defaults, so the same code runs
locally (a .env file) and on Vercel (project environment variables) and can be
tuned without a redeploy. Nothing here touches the network or the working directory:
asset paths are anchored to this file, which is what lets them resolve inside a
serverless bundle.

Environment variables
    ANTHROPIC_API_KEY             required to run an annotation (checked lazily)
    CANETRUST_MODEL               default claude-sonnet-4-6
    CANETRUST_EFFORT              default high (low / medium / high / xhigh / max)
    CANETRUST_MAX_TOKENS_SINGLE   default 8000
    CANETRUST_MAX_TOKENS_MULTI    default 48000
    CANETRUST_ACCESS_CODE         shared code the web app requires (see api/index.py)
    CANETRUST_ALLOW_OPEN          1 = let a deployment run with NO access code (temporary, for
                                  testing behind another gate; the default refuses)
    CANETRUST_MAX_UPLOAD_MB       default 4.0 (Vercel rejects request bodies over 4.5 MB)
    CANETRUST_MAX_PAGES           default 250 pages per PDF
    CANETRUST_MAX_CHARS           default 800000 characters of text per packet
    CANETRUST_DEADLINE_SECONDS    default 280 (stay under the function's maxDuration)
    CANETRUST_DEBUG_DIR           if set, the model's raw JSON is written there
"""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    model: str
    effort: str
    max_tokens_single: int
    max_tokens_multi: int
    layer_status: str
    access_code: str | None
    allow_open: bool
    max_upload_bytes: int
    max_pages: int
    max_chars: int
    deadline_seconds: int
    debug_dir: Path | None
    template_single: Path
    template_multi: Path
    rules_single: Path
    rules_multi: Path


def load_local_env() -> None:
    """Read a local .env file, but never on Vercel, where the dashboard's environment
    variables are the source of truth (a stray .env must not override them).
    override=True: a stale variable exported in the shell must not shadow .env."""
    if os.getenv("VERCEL"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def load_settings() -> Settings:
    debug_dir = os.getenv("CANETRUST_DEBUG_DIR")
    max_mb = float(os.getenv("CANETRUST_MAX_UPLOAD_MB") or 4.0)
    return Settings(
        api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        model=os.getenv("CANETRUST_MODEL") or "claude-sonnet-4-6",
        effort=os.getenv("CANETRUST_EFFORT") or "high",
        max_tokens_single=_int("CANETRUST_MAX_TOKENS_SINGLE", 8000),
        max_tokens_multi=_int("CANETRUST_MAX_TOKENS_MULTI", 48000),
        layer_status="OPERATIVE",
        access_code=os.getenv("CANETRUST_ACCESS_CODE") or None,
        allow_open=(os.getenv("CANETRUST_ALLOW_OPEN") or "").strip().lower() in ("1", "true", "yes"),
        max_upload_bytes=int(max_mb * 1024 * 1024),
        max_pages=_int("CANETRUST_MAX_PAGES", 250),
        max_chars=_int("CANETRUST_MAX_CHARS", 800_000),
        deadline_seconds=_int("CANETRUST_DEADLINE_SECONDS", 280),
        debug_dir=Path(debug_dir) if debug_dir else None,
        template_single=ASSETS / "MiLA_Annotation_Template_v0.xlsx",
        template_multi=ASSETS / "MiLA_Annotation_Template_Amendments_v0.xlsx",
        rules_single=ASSETS / "extraction_rules.md",
        rules_multi=ASSETS / "extraction_rules_multi.md",
    )


def require_api_key(settings: Settings) -> str:
    if not settings.api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env locally, or to the project's "
            "environment variables on Vercel."
        )
    return settings.api_key
