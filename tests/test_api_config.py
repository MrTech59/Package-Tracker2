"""Tests for the HTTP config/health payloads.

These assert on the route functions directly rather than through a live
server, so they stay fast and dependency-free. The important contract here is
that /api/config advertises the running version: the dashboard shows it so a
stale deployment (old code after an update) is obvious at a glance.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import __version__  # noqa: E402
from app import main as main_module  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import TrackerService  # noqa: E402


class _Bare:
    """Stands in for the FastAPI app object just enough for the routes."""

    def __init__(self):
        self.state = type("S", (), {})()


class TestConfigPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.tmp.name) / "api.db",
            data_dir=Path(self.tmp.name),
        )
        self.db = Database(self.settings.db_path)
        self.service = TrackerService(self.settings, self.db)

        self._orig_app = main_module.app
        bare = _Bare()
        bare.state.service = self.service
        bare.state.db = self.db
        main_module.app = bare
        # get_config/health read the module-level settings object.
        self._orig_settings = main_module.settings
        main_module.settings = self.settings

    def tearDown(self):
        main_module.app = self._orig_app
        main_module.settings = self._orig_settings
        self.db.close()
        self.tmp.cleanup()

    def test_config_reports_version(self):
        payload = asyncio.run(main_module.get_config())
        self.assertEqual(payload["version"], __version__)
        # Still carries the fields the frontend depends on.
        for key in ("carriers", "statuses", "scrape_enabled", "mcp_url",
                    "poll_interval_seconds"):
            self.assertIn(key, payload)

    def test_config_version_matches_health(self):
        cfg = asyncio.run(main_module.get_config())
        health = asyncio.run(main_module.health())
        self.assertEqual(cfg["version"], health["version"])
        self.assertEqual(cfg["version"], __version__)

    def test_version_is_a_dotted_string(self):
        payload = asyncio.run(main_module.get_config())
        parts = str(payload["version"]).split(".")
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(p.isdigit() for p in parts), payload["version"])


class TestStaticVersionConsistency(unittest.TestCase):
    """index.html asset cache-busters must match the package version."""

    def test_asset_query_matches_version(self):
        html = (Path(__file__).resolve().parent.parent
                / "app" / "static" / "index.html").read_text(encoding="utf-8")
        expect = f"?v={__version__}"
        self.assertIn(f"styles.css{expect}", html)
        self.assertIn(f"app.js{expect}", html)

    def test_dashboard_references_version_elements(self):
        root = Path(__file__).resolve().parent.parent / "app" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")

        # The dialog row and the brand badge must exist and be wired up.
        self.assertIn('id="diag-version"', html)
        self.assertIn('id="brand-version"', html)
        self.assertIn("renderVersionDiag", js)
        self.assertIn("brand-version", js)
        self.assertIn("cfg.version", js)


class TestHiddenElementsStayHidden(unittest.TestCase):
    """`.settings-actions { display: flex }` used to beat [hidden].

    A class selector outranks the user-agent `[hidden] { display: none }`, so
    `element.hidden = true` had no visual effect on buttons inside
    .settings-actions -- the Unpair button showed even when nothing was
    paired. Guard against that regression.
    """

    @classmethod
    def setUpClass(cls):
        css_path = (Path(__file__).resolve().parent.parent
                    / "app" / "static" / "styles.css")
        cls.css = css_path.read_text(encoding="utf-8")
        js_path = (Path(__file__).resolve().parent.parent
                   / "app" / "static" / "app.js")
        cls.js = js_path.read_text(encoding="utf-8")

    def test_hidden_rule_is_important(self):
        self.assertRegex(
            self.css,
            r"\[hidden\]\s*\{\s*display:\s*none\s*!important",
            "styles.css must force [hidden] { display: none !important }",
        )

    def test_no_duplicate_pair_manual_hidden_rule(self):
        # The override now lives in one place; a second copy invites drift.
        self.assertNotIn(".pair-manual[hidden]", self.css)

    def test_no_token_state_keeps_buttons_clickable(self):
        # Pressing them must point at the token box, not sit dead.
        block = self._branch("if (!p.has_token)")
        self.assertIn("pairAuto.disabled = false", block)
        self.assertIn("pairManual.disabled = false", block)
        self.assertIn("unpair.hidden = true", block)
        # The prompt is now the in-page token field, not a .env instruction.
        self.assertIn("bot token", block)
        self.assertIn("pair-token", self.js)

    def test_pair_handlers_guard_on_missing_token(self):
        self.assertIn("showMissingTokenHelp", self.js)
        # Both entry points bail out to the helper rather than failing later.
        self.assertGreaterEqual(self.js.count("if (state.pairing && !state.pairing.has_token)"), 2)

    def _branch(self, marker: str) -> str:
        start = self.js.index(marker)
        end = self.js.index("return;", start)
        return self.js[start:end]


if __name__ == "__main__":
    unittest.main(verbosity=2)
