"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader; does not override already-set variables."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def dotenv_path() -> Path:
    """Where the runtime .env lives (repo root in dev, /opt/... when installed)."""
    override = os.environ.get("DOTENV_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


def _quote_env(value: str) -> str:
    """Render a value so _load_dotenv reads it back unchanged."""
    if value == "" or any(c in value for c in ' \t#"\'$`\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def save_env_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """Create or update keys in the .env file, preserving everything else.

    Comments, blank lines, and the ordering of existing keys are kept so a
    hand-edited file is not mangled. Values are written back inside quotes
    when they contain characters a shell would otherwise eat.
    """
    target = path or dotenv_path()
    remaining = dict(updates)

    lines: list[str] = []
    if target.is_file():
        lines = target.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={_quote_env(remaining.pop(key))}")
                continue
        out.append(line)

    # Anything not already present is appended under a clear heading.
    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={_quote_env(value)}")

    target.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(out).rstrip("\n") + "\n"
    target.write_text(text, encoding="utf-8")
    # The token lives here, so keep it readable only by the owner.
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return target


@dataclass
class Settings:
    """Runtime settings for the tracker."""

    # --- Web server ---
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Storage ---
    db_path: Path = field(default_factory=lambda: Path("data/tracker.db"))
    data_dir: Path = field(default_factory=lambda: Path("data"))

    # --- MCP browser server ---
    # The LXC points at the picoclaw-browser MCP endpoint.
    mcp_url: str = "http://192.168.3.137:8000/mcp"
    mcp_protocol_version: str = "2024-11-05"
    mcp_timeout_seconds: float = 60.0

    # --- Scraper tuning ---
    scrape_enabled: bool = True
    # Seconds between automatic status polls per package.
    poll_interval_seconds: int = 3600
    # Do not re-scrape the same package more often than this regardless of requests.
    min_scrape_gap_seconds: int = 300
    # Max characters of page text handed to the status parser.
    max_page_chars: int = 100_000

    # --- Telegram ---
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout_seconds: float = 15.0
    # Overridable so a local Bot API server (or a test double) can be used.
    telegram_api_base: str = "https://api.telegram.org"

    # --- Auth (optional, off by default) ---
    # When set, all /api routes require header `X-Api-Key: <value>`.
    api_key: str = ""

    @classmethod
    def load(cls, dotenv: Path | None = None) -> "Settings":
        if dotenv is not None:
            _load_dotenv(dotenv)

        data_dir = Path(os.environ.get("DATA_DIR", "data"))

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        tg_enabled = _env_bool("TELEGRAM_ENABLED", bool(token and chat_id))

        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8080")),
            db_path=Path(os.environ.get("DB_PATH", str(data_dir / "tracker.db"))),
            data_dir=data_dir,
            mcp_url=os.environ.get("MCP_URL", "http://192.168.3.137:8000/mcp"),
            mcp_protocol_version=os.environ.get("MCP_PROTOCOL_VERSION", "2024-11-05"),
            mcp_timeout_seconds=float(os.environ.get("MCP_TIMEOUT_SECONDS", "60")),
            scrape_enabled=_env_bool("SCRAPE_ENABLED", True),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "3600")),
            min_scrape_gap_seconds=int(os.environ.get("MIN_SCRAPE_GAP_SECONDS", "300")),
            max_page_chars=int(os.environ.get("MAX_PAGE_CHARS", "100000")),
            telegram_enabled=tg_enabled,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            telegram_timeout_seconds=float(os.environ.get("TELEGRAM_TIMEOUT_SECONDS", "15")),
            telegram_api_base=os.environ.get(
                "TELEGRAM_API_BASE", "https://api.telegram.org"
            ).rstrip("/"),
            api_key=os.environ.get("API_KEY", "").strip(),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings.load(dotenv_path())
