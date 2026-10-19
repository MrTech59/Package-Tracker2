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
            api_key=os.environ.get("API_KEY", "").strip(),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings.load(Path(__file__).resolve().parent.parent / ".env")
