"""Tests for saving the Telegram bot token from the dashboard.

The point of this feature is that a user never has to edit .env by hand, so
the two things worth guarding are: the token survives a restart (persisted in
both the database and .env), and a bad token is rejected instead of being
stored and silently failing later.
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, save_env_values  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import TrackerService  # noqa: E402

GOOD_TOKEN = "123456789:ABCdefGhIJKlmnoPQRsTUVwxyZ"

REPO_ENV = Path(__file__).resolve().parent.parent / ".env"


def _guarded_env_writer(updates, path=None):
    """A save_env_values that refuses to touch the repository's real .env.

    set_bot_token() falls back to dotenv_path() when no path is given, and the
    dev .env lives at the repo root, so an unguarded test run would rewrite the
    developer's own configuration. Fail loudly instead.
    """
    target = Path(path) if path is not None else None
    if target is None or target.resolve() == REPO_ENV.resolve():
        raise AssertionError(
            f"test tried to write the real .env ({REPO_ENV}); pass an explicit path"
        )
    from app.config import save_env_values as real

    return real(updates, target)

class _TmpSettings(Settings):
    """Settings pointed at a throwaway directory."""

    @classmethod
    def make(cls, tmp: str) -> "_TmpSettings":
        base = Path(tmp)
        return cls(
            db_path=base / "tracker.db",
            data_dir=base,
            telegram_enabled=False,
            telegram_bot_token="",
            telegram_chat_id="",
        )


class TestSaveEnvValues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_file_when_missing(self):
        save_env_values({"TELEGRAM_BOT_TOKEN": GOOD_TOKEN}, self.env)
        self.assertTrue(self.env.is_file())
        self.assertIn(f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}", self.env.read_text("utf-8"))

    def test_updates_existing_key_in_place(self):
        self.env.write_text(
            "# leading comment\nPORT=8080\nTELEGRAM_BOT_TOKEN=old\nMCP_URL=http://x/mcp\n",
            encoding="utf-8",
        )
        save_env_values({"TELEGRAM_BOT_TOKEN": GOOD_TOKEN}, self.env)
        text = self.env.read_text("utf-8")
        self.assertIn(f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}", text)
        self.assertNotIn("old", text)
        # Everything else, including comments and ordering, is preserved.
        self.assertIn("# leading comment", text)
        self.assertIn("PORT=8080", text)
        self.assertIn("MCP_URL=http://x/mcp", text)
        self.assertLess(text.index("PORT=8080"), text.index("TELEGRAM_BOT_TOKEN"))

    def test_appends_new_keys(self):
        self.env.write_text("PORT=8080\n", encoding="utf-8")
        save_env_values({"TELEGRAM_BOT_TOKEN": GOOD_TOKEN}, self.env)
        lines = self.env.read_text("utf-8").splitlines()
        self.assertEqual(lines[0], "PORT=8080")
        self.assertIn(f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}", lines)

    def test_quotes_values_with_spaces(self):
        save_env_values({"NOTE": "hello world"}, self.env)
        self.assertIn('NOTE="hello world"', self.env.read_text("utf-8"))

    def test_file_is_owner_only(self):
        save_env_values({"TELEGRAM_BOT_TOKEN": GOOD_TOKEN}, self.env)
        if os.name == "posix":
            self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_round_trips_through_the_loader(self):
        """A value we write must read back identically."""
        from app.config import _load_dotenv

        save_env_values({"TELEGRAM_BOT_TOKEN": GOOD_TOKEN}, self.env)
        with mock.patch.dict(os.environ, {}, clear=True):
            _load_dotenv(self.env)
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], GOOD_TOKEN)


class TestSetBotToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.settings = _TmpSettings.make(self.tmp.name)
        self.db = Database(self.settings.db_path)
        self.service = TrackerService(self.settings, self.db)
        self.env = self.base / ".env"
        # An ambient TELEGRAM_BOT_TOKEN would leak into the fallbacks under
        # test, so blank the environment for the duration of each test.
        self._env_patch = mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def _save(self, token=GOOD_TOKEN):
        # Keep the test hermetic: never write to the repo's real .env.
        with self._env_writer(self.env):
            return self.service.set_bot_token(token)

    @contextlib.contextmanager
    def _env_writer(self, target):
        """Redirect save_env_values to a temp file for the duration."""
        with mock.patch(
            "app.service.save_env_values",
            side_effect=lambda updates, path=None: _guarded_env_writer(updates, target),
        ):
            yield

    def test_saves_to_db_and_env(self):
        result = self._save()
        self.assertTrue(result["saved_to_env"])
        self.assertIn(GOOD_TOKEN, self.env.read_text("utf-8"))
        self.assertEqual(self.db.get_meta(self.service.TELEGRAM_TOKEN_META_KEY), GOOD_TOKEN)

    def test_applies_to_live_notifier(self):
        self._save()
        self.assertEqual(self.service.notifier.settings.telegram_bot_token, GOOD_TOKEN)
        self.assertTrue(self.service.pairing_status()["has_token"])

    def test_rejects_empty_token(self):
        with self.assertRaises(ValueError):
            self.service.set_bot_token("   ")

    def test_rejects_malformed_token(self):
        for bad in ("not-a-token", "abc:def", ":leadingcolon", "123456789"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.service.set_bot_token(bad)

    def test_env_write_failure_is_reported_not_raised(self):
        with mock.patch("app.service.save_env_values", side_effect=OSError("read-only fs")):
            result = self.service.set_bot_token(GOOD_TOKEN)
        self.assertFalse(result["saved_to_env"])
        self.assertIn("read-only", result["env_error"])
        # The token still works for this session.
        self.assertEqual(self.service.notifier.settings.telegram_bot_token, GOOD_TOKEN)

    def test_token_survives_restart(self):
        self._save()
        restored = TrackerService(self.settings, self.db)
        self.assertEqual(restored.notifier.settings.telegram_bot_token, GOOD_TOKEN)
        self.assertTrue(restored.pairing_status()["has_token"])

    def test_clear_falls_back_to_env(self):
        self._save()
        self.service.clear_bot_token()
        self.assertEqual(self.service.notifier.settings.telegram_bot_token, "")
        self.assertFalse(self.service.pairing_status()["has_token"])

    def test_clear_restores_the_configured_token(self):
        """Regression: the notifier shares the Settings object.

        Applying a dashboard token used to overwrite the configured one, so
        clearing read its own value back and appeared to do nothing.
        """
        self.settings.telegram_bot_token = "111:FromEnv"
        service = TrackerService(self.settings, self.db)
        service._env_bot_token = "111:FromEnv"

        with self._env_writer(self.env):
            service.set_bot_token(GOOD_TOKEN)
        self.assertEqual(service.notifier.settings.telegram_bot_token, GOOD_TOKEN)

        service.clear_bot_token()
        self.assertEqual(service.notifier.settings.telegram_bot_token, "111:FromEnv")
        self.assertEqual(service.pairing_status()["token_source"], "env")

    def test_env_token_is_used_when_nothing_saved(self):
        settings = _TmpSettings.make(self.tmp.name)
        settings.telegram_bot_token = "999:SavedInEnv"
        service = TrackerService(settings, self.db)
        status = service.pairing_status()
        self.assertTrue(status["has_token"])
        self.assertEqual(status["token_source"], "env")

    def test_dashboard_token_reported_as_source(self):
        self._save()
        status = self.service.pairing_status()
        self.assertEqual(status["token_source"], "dashboard")
        # Only a short suffix is ever exposed, never the whole token.
        self.assertTrue(status["token_hint"].startswith("…"))
        self.assertEqual(status["token_hint"], "…" + GOOD_TOKEN[-4:])
        self.assertNotIn(GOOD_TOKEN, str(status))


class TestTokenEndpointValidation(unittest.TestCase):
    """The route layer must translate ValueError into a 400, not a 500."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _TmpSettings.make(self.tmp.name)
        self.db = Database(self.settings.db_path)
        self.service = TrackerService(self.settings, self.db)

        from app import main as main_module

        self.main = main_module
        self._orig_app = main_module.app
        bare = type("B", (), {})()
        bare.state = type("S", (), {})()
        bare.state.service = self.service
        bare.state.db = self.db
        main_module.app = bare
        self._env_patch = mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.main.app = self._orig_app
        self.db.close()
        self.tmp.cleanup()

    @contextlib.contextmanager
    def _env_writer(self, target=None):
        target = target or (Path(self.tmp.name) / ".env")
        with mock.patch(
            "app.service.save_env_values",
            side_effect=lambda updates, path=None: _guarded_env_writer(updates, target),
        ):
            yield

    def test_bad_token_raises_http_400(self):
        import asyncio

        from fastapi import HTTPException

        payload = self.main.TokenIn(bot_token="nonsense")
        with self._env_writer():
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(self.main.telegram_set_token(payload))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_good_token_is_verified_and_rejected_when_telegram_says_no(self):
        import asyncio

        from fastapi import HTTPException

        payload = self.main.TokenIn(bot_token=GOOD_TOKEN)
        with mock.patch.object(
            self.service.notifier, "check_connection",
            return_value={"ok": False, "error": "401 Unauthorized"},
        ):
            with self._env_writer():
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(self.main.telegram_set_token(payload))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("401", ctx.exception.detail)
        # A token that fails verification must not be kept.
        self.assertEqual(self.service.notifier.settings.telegram_bot_token, "")

    def test_good_token_accepted_when_telegram_confirms(self):
        import asyncio

        from fastapi import HTTPException  # noqa: F401

        payload = self.main.TokenIn(bot_token=GOOD_TOKEN)
        with mock.patch.object(
            self.service.notifier, "check_connection",
            return_value={"ok": True, "bot": "my_test_bot"},
        ):
            with self._env_writer():
                result = asyncio.run(self.main.telegram_set_token(payload))
        self.assertTrue(result["ok"])
        self.assertEqual(result["bot"], "my_test_bot")
        self.assertTrue(result["pairing"]["has_token"])

    def test_manual_pair_accepts_a_token_inline(self):
        import asyncio

        with self._env_writer():
            result = asyncio.run(
                self.main.telegram_pair_manual(
                    self.main.PairIn(chat_id="555", name="Connor", bot_token=GOOD_TOKEN)
                )
            )
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.notifier.settings.telegram_bot_token, GOOD_TOKEN)


class TestFrontendTokenUi(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "app" / "static"
        self.html = (root / "index.html").read_text(encoding="utf-8")
        self.js = (root / "app.js").read_text(encoding="utf-8")
        self.css = (root / "styles.css").read_text(encoding="utf-8")

    def test_token_input_exists(self):
        self.assertIn('id="pair-token"', self.html)
        self.assertIn('id="btn-pair-token-save"', self.html)
        self.assertIn('id="btn-pair-token-clear"', self.html)

    def test_token_field_is_masked(self):
        start = self.html.index('id="pair-token"')
        element = self.html[start - 200:start + 200]
        self.assertIn('type="password"', element)

    def test_handlers_are_wired(self):
        self.assertIn("async function saveBotToken", self.js)
        self.assertIn("async function forgetBotToken", self.js)
        self.assertIn('$("btn-pair-token-save").addEventListener', self.js)
        self.assertIn('$("btn-pair-token-clear").addEventListener', self.js)

    def test_never_reintroduces_env_only_instructions_as_the_primary_path(self):
        """The old dead-end told users to edit .env; the box replaces it."""
        self.assertNotIn("nano /opt/package-tracker/.env", self.js)
        self.assertIn("showMissingTokenHelp", self.js)

    def test_token_styles_exist(self):
        for selector in (".pair-token", ".pair-token-row", ".pair-token-help"):
            self.assertIn(selector, self.css)


class TestTelegramApiBase(unittest.TestCase):
    """The Bot API root is configurable, which is what makes this testable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _TmpSettings.make(self.tmp.name)
        self.db = Database(self.settings.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_defaults_to_the_public_api(self):
        self.assertEqual(self.settings.telegram_api_base, "https://api.telegram.org")

    def test_env_override_is_read_and_trailing_slash_stripped(self):
        with mock.patch.dict(
            os.environ, {"TELEGRAM_API_BASE": "http://127.0.0.1:8099/"}, clear=False
        ):
            loaded = Settings.load()
        self.assertEqual(loaded.telegram_api_base, "http://127.0.0.1:8099")

    def test_notifier_builds_urls_against_the_override(self):
        from app.notifier import TelegramNotifier

        self.settings.telegram_bot_token = GOOD_TOKEN
        self.settings.telegram_api_base = "http://127.0.0.1:8099"
        notifier = TelegramNotifier(self.settings)
        self.assertEqual(
            notifier._base, f"http://127.0.0.1:8099/bot{GOOD_TOKEN}"
        )

    def test_no_source_file_hardcodes_the_api_host(self):
        """The notifier must build URLs from the configurable base."""
        notifier = (Path(__file__).resolve().parent.parent
                    / "app" / "notifier.py").read_text(encoding="utf-8")
        for i, line in enumerate(notifier.splitlines(), 1):
            if "api.telegram.org" in line:
                self.assertIn(
                    "_base", line,
                    f"notifier.py:{i} hardcodes the Telegram host outside _base",
                )


class TestUnauthorizedMessage(unittest.TestCase):
    """A bare 'Unauthorized' is not actionable; the 401 path must explain."""

    def test_check_connection_translates_401(self):
        import asyncio

        from app.notifier import TelegramNotifier

        settings = Settings(telegram_bot_token=GOOD_TOKEN, telegram_api_base="http://127.0.0.1:1")
        notifier = TelegramNotifier(settings)

        class _Resp:
            status_code = 401

            @staticmethod
            def json():
                return {"ok": False, "error_code": 401, "description": "Unauthorized"}

        async def fake_get(self, url, **kwargs):  # noqa: ANN001
            return _Resp()

        with mock.patch("httpx.AsyncClient.get", new=fake_get):
            result = asyncio.run(notifier.check_connection())

        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertIn("BotFather", result["error"])
        # The raw Telegram string alone is not good enough.
        self.assertNotEqual(result["error"].strip(), "Unauthorized")

    def test_explain_error_mentions_the_dashboard_flow(self):
        from app.notifier import TelegramNotifier

        message = TelegramNotifier._explain_error({"error_code": 401}, 401)
        self.assertIn("Save token", message)
        self.assertNotIn("your .env file", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
