"""Tests for the database layer and service logic."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import Status  # noqa: E402
from app.notifier import TelegramNotifier  # noqa: E402
from app.service import TrackerService  # noqa: E402


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_create_and_get(self):
        pkg = self.db.create_package(name="Test", tracking_number="ABC123", carrier="ups")
        self.assertIsNotNone(pkg["id"])
        self.assertEqual(pkg["name"], "Test")
        self.assertEqual(pkg["status"], Status.ORDERED.value)

        fetched = self.db.get_package(pkg["id"])
        self.assertEqual(fetched["tracking_number"], "ABC123")

    def test_create_logs_event(self):
        pkg = self.db.create_package(name="Test")
        events = self.db.list_events(pkg["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "Package created")

    def test_update(self):
        pkg = self.db.create_package(name="Test")
        updated = self.db.update_package(pkg["id"], status=Status.SHIPPED.value, notes="hi")
        self.assertEqual(updated["status"], Status.SHIPPED.value)
        self.assertEqual(updated["notes"], "hi")

    def test_update_ignores_unknown_fields(self):
        pkg = self.db.create_package(name="Test")
        updated = self.db.update_package(pkg["id"], evil="drop table packages")
        self.assertEqual(updated["name"], "Test")

    def test_delete_cascades_events(self):
        pkg = self.db.create_package(name="Test")
        self.db.add_event(pkg["id"], Status.SHIPPED, "moved")
        self.assertTrue(self.db.delete_package(pkg["id"]))
        self.assertIsNone(self.db.get_package(pkg["id"]))
        self.assertEqual(self.db.list_events(pkg["id"]), [])

    def test_find_by_tracking_case_insensitive(self):
        self.db.create_package(name="Test", tracking_number="AbC123")
        found = self.db.find_by_tracking("abc123")
        self.assertIsNotNone(found)
        self.assertEqual(found["name"], "Test")

    def test_persistence_across_connections(self):
        pkg = self.db.create_package(name="Persist")
        reopened = Database(self.db.path)
        self.assertEqual(reopened.get_package(pkg["id"])["name"], "Persist")
        reopened.close()

    def test_meta(self):
        self.assertIsNone(self.db.get_meta("x"))
        self.db.set_meta("x", "1")
        self.assertEqual(self.db.get_meta("x"), "1")
        self.db.set_meta("x", "2")
        self.assertEqual(self.db.get_meta("x"), "2")


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(db_path=Path(self.tmp.name) / "svc.db",
                                 data_dir=Path(self.tmp.name))
        self.db = Database(self.settings.db_path)
        self.service = TrackerService(self.settings, self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_decorate_adds_fields(self):
        pkg = self.service.create_package({"name": "Thing", "carrier": "ups",
                                           "tracking_number": "1Z999"})
        self.assertEqual(pkg["carrier_name"], "UPS")
        self.assertTrue(pkg["has_tracking"])
        self.assertIn("1Z999", pkg["tracking_url"])
        self.assertFalse(pkg["status_is_terminal"])

    def test_set_status_sets_delivered_at(self):
        pkg = self.service.create_package({"name": "Thing"})
        updated, meta = self.service.set_status(pkg["id"], Status.DELIVERED)
        self.assertIsNotNone(updated["delivered_at"])
        self.assertTrue(meta["changed"])
        self.assertEqual(meta["old_status"], Status.ORDERED)

    def test_set_status_clears_delivered_at_on_regression(self):
        pkg = self.service.create_package({"name": "Thing"})
        self.service.set_status(pkg["id"], Status.DELIVERED)
        updated, _ = self.service.set_status(pkg["id"], Status.IN_TRANSIT)
        self.assertIsNone(updated["delivered_at"])

    def test_stats(self):
        self.service.create_package({"name": "A", "status": "in_transit"})
        self.service.create_package({"name": "B", "status": "delivered"})
        self.service.create_package({"name": "C", "status": "exception"})
        stats = self.service.stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["in_flight"], 1)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(stats["problems"], 1)

    def test_invalid_status_falls_back(self):
        pkg = self.service.create_package({"name": "A", "status": "nonsense"})
        self.assertEqual(pkg["status"], Status.ORDERED.value)

    def test_update_package_resolves_carrier(self):
        pkg = self.service.create_package({"name": "A"})
        updated = self.service.update_package(pkg["id"], {"carrier": "United States Postal Service"})
        self.assertEqual(updated["carrier_id"], "usps")

    def test_refresh_without_tracking(self):
        import asyncio

        pkg = self.service.create_package({"name": "A"})
        result = asyncio.run(self.service.refresh_package(pkg["id"]))
        self.assertFalse(result["ok"])
        self.assertIn("no tracking number", result["error"].lower())

    def test_refresh_disabled(self):
        import asyncio

        self.settings.scrape_enabled = False
        pkg = self.service.create_package({"name": "A", "tracking_number": "X", "carrier": "ups"})
        result = asyncio.run(self.service.refresh_package(pkg["id"]))
        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["error"].lower())


class TestTelegramPairing(unittest.TestCase):
    """Pairing persists a chat id and flips the live notifier over to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            db_path=Path(self.tmp.name) / "pair.db",
            data_dir=Path(self.tmp.name),
            telegram_bot_token="123:FAKE",
            telegram_chat_id="",
            telegram_enabled=False,
        )
        self.db = Database(self.settings.db_path)
        self.service = TrackerService(self.settings, self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_not_paired_initially(self):
        status = self.service.pairing_status()
        self.assertTrue(status["has_token"])
        self.assertFalse(status["paired"])
        self.assertFalse(status["notifications_enabled"])
        self.assertEqual(status["chat_id"], "")

    def test_apply_pairing_persists_and_enables(self):
        result = self.service.apply_pairing("5551234", name="Connor")
        self.assertEqual(result["chat_id"], "5551234")
        self.assertTrue(result["notifications_enabled"])

        status = self.service.pairing_status()
        self.assertTrue(status["paired"])
        self.assertTrue(status["paired_via_dashboard"])
        self.assertFalse(status["paired_from_env"])
        self.assertEqual(status["chat_name"], "Connor")
        self.assertTrue(status["notifications_enabled"])

    def test_pairing_survives_restart(self):
        self.service.apply_pairing("5551234", name="Connor")

        # Fresh Database + service, as if the process restarted.
        db2 = Database(self.settings.db_path)
        try:
            settings2 = Settings(
                db_path=self.settings.db_path,
                data_dir=self.settings.data_dir,
                telegram_bot_token="123:FAKE",
                telegram_chat_id="",
                telegram_enabled=False,
            )
            service2 = TrackerService(settings2, db2)
            # Before load_paired_chat() the live notifier has no chat, but the
            # value is already durable in the database.
            self.assertFalse(service2.notifier.enabled)
            self.assertTrue(service2.pairing_status()["paired_via_dashboard"])

            loaded = service2.load_paired_chat()
            self.assertEqual(loaded, "5551234")
            self.assertEqual(service2.notifier.settings.telegram_chat_id, "5551234")
            self.assertEqual(service2.telegram_chat_id(), "5551234")
            self.assertTrue(service2.notifier.enabled)
        finally:
            db2.close()

    def test_auto_pair_loads_persisted_chat(self):
        # Nothing persisted -> load returns None and nothing changes.
        self.assertIsNone(self.service.load_paired_chat())

    def test_unpair_clears_dashboard_pairing(self):
        self.service.apply_pairing("5551234", name="Connor")
        self.service.unpair()

        status = self.service.pairing_status()
        self.assertFalse(status["paired_via_dashboard"])
        self.assertEqual(status["chat_id"], "")
        self.assertFalse(status["notifications_enabled"])

    def test_unpair_falls_back_to_env_chat_id(self):
        import os

        os.environ["TELEGRAM_CHAT_ID"] = "999888"
        try:
            # Must be built *after* the env var is set, since the service
            # captures the configured chat id at construction time.
            service = TrackerService(self.settings, self.db)
            self.assertEqual(service.telegram_chat_id(), "999888")

            service.apply_pairing("5551234")
            self.assertEqual(service.telegram_chat_id(), "5551234")

            service.unpair()
            # Falls back to the .env value rather than going blank.
            self.assertEqual(service.telegram_chat_id(), "999888")
            self.assertEqual(service.notifier.settings.telegram_chat_id, "999888")
            self.assertFalse(service.pairing_status()["paired_via_dashboard"])
        finally:
            del os.environ["TELEGRAM_CHAT_ID"]

    def test_apply_pairing_rejects_blank(self):
        with self.assertRaises(ValueError):
            self.service.apply_pairing("   ")

    def test_pairing_overrides_env_chat_id(self):
        # A service configured with an env chat id, then paired via dashboard.
        service = TrackerService(self.settings, self.db)
        service._env_chat_id = "111"
        service.notifier.settings.telegram_chat_id = "111"

        service.apply_pairing("222")
        self.assertEqual(service.telegram_chat_id(), "222")
        self.assertEqual(service.notifier.settings.telegram_chat_id, "222")

        status = service.pairing_status()
        self.assertTrue(status["paired_via_dashboard"])
        self.assertFalse(status["paired_from_env"])

    def test_env_only_pairing_reported_as_env(self):
        service = TrackerService(self.settings, self.db)
        service._env_chat_id = "111"
        service.notifier.settings.telegram_chat_id = "111"
        service.notifier.settings.telegram_enabled = True

        status = service.pairing_status()
        self.assertTrue(status["paired"])
        self.assertFalse(status["paired_via_dashboard"])
        self.assertTrue(status["paired_from_env"])
        self.assertTrue(status["notifications_enabled"])


class TestPairingHelpers(unittest.TestCase):
    """Static parsing helpers used by the pairing flow."""

    def test_is_pair_command(self):
        for text in ("/start", "/START", "/pair", "/pair now", "/subscribe", "  /start  "):
            self.assertTrue(TelegramNotifier.is_pair_command(text), text)

    def test_is_not_pair_command(self):
        for text in ("hello", "", "/startx", "start", "where is my package"):
            self.assertFalse(TelegramNotifier.is_pair_command(text), text)

    def test_describe_update_private_chat(self):
        update = {
            "update_id": 7,
            "message": {
                "chat": {"id": 42, "type": "private"},
                "from": {"first_name": "Connor", "last_name": "O", "username": "connor"},
                "text": "/start",
                "date": 1700000000,
            },
        }
        described = TelegramNotifier.describe_update(update)
        self.assertEqual(described["chat_id"], "42")
        self.assertEqual(described["name"], "Connor O")
        self.assertEqual(described["username"], "connor")
        self.assertEqual(described["text"], "/start")
        self.assertEqual(described["update_id"], 7)

    def test_describe_update_group_uses_title(self):
        update = {
            "update_id": 8,
            "message": {
                "chat": {"id": -100, "type": "group", "title": "Home"},
                "from": {"first_name": "Connor"},
                "text": "hi",
            },
        }
        described = TelegramNotifier.describe_update(update)
        self.assertEqual(described["name"], "Home")
        self.assertEqual(described["chat_id"], "-100")

    def test_describe_update_ignores_non_messages(self):
        self.assertIsNone(TelegramNotifier.describe_update({"update_id": 9}))
        self.assertIsNone(TelegramNotifier.describe_update({"update_id": 9, "message": {}}))

    def test_describe_update_handles_channel_post(self):
        update = {
            "update_id": 10,
            "channel_post": {
                "chat": {"id": -500, "type": "channel", "title": "Alerts"},
                "text": "hello",
            },
        }
        described = TelegramNotifier.describe_update(update)
        self.assertEqual(described["chat_id"], "-500")
        self.assertEqual(described["name"], "Alerts")


class TestFindChat(unittest.TestCase):
    """find_chat's offset bookkeeping, with the HTTP layer stubbed out."""

    def setUp(self):
        self.settings = Settings(telegram_bot_token="123:FAKE")
        self.notifier = TelegramNotifier(self.settings)

    def test_pair_command_wins_and_acknowledges_offset(self):
        import asyncio

        calls = []

        async def fake_updates(offset=None):
            calls.append(offset)
            if offset == -1:
                return {"ok": True, "updates": [{"update_id": 100, "message": {
                    "chat": {"id": 1, "type": "private"}, "text": "old"}}]}
            return {"ok": True, "updates": [{
                "update_id": 101,
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"first_name": "Connor"},
                    "text": "/start",
                },
            }]}

        self.notifier.get_updates = fake_updates
        found = asyncio.run(self.notifier.find_chat(timeout=5))
        self.assertTrue(found["ok"])
        self.assertEqual(found["chat"]["chat_id"], "42")
        self.assertEqual(found["paired_by"], "command")
        # Baseline established at -1, then resumes after the last seen update.
        self.assertEqual(calls[0], -1)
        self.assertEqual(calls[1], 101)

    def test_plain_message_pairs_as_fallback(self):
        import asyncio

        async def fake_updates(offset=None):
            if offset == -1:
                return {"ok": True, "updates": []}
            return {"ok": True, "updates": [{
                "update_id": 5,
                "message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"first_name": "Ada"},
                    "text": "hello there",
                },
            }]}

        self.notifier.get_updates = fake_updates
        found = asyncio.run(self.notifier.find_chat(timeout=5))
        self.assertTrue(found["ok"])
        self.assertEqual(found["chat"]["chat_id"], "77")
        self.assertEqual(found["paired_by"], "message")

    def test_no_message_times_out(self):
        import asyncio

        async def fake_updates(offset=None):
            return {"ok": True, "updates": []}

        self.notifier.get_updates = fake_updates
        found = asyncio.run(self.notifier.find_chat(timeout=0.1))
        self.assertFalse(found["ok"])
        self.assertIn("No new message", found["error"])

    def test_existing_start_in_backlog_pairs_immediately(self):
        """The user usually sends /start *before* pressing Pair.

        Regression: the baseline call (offset=-1) used to consume that
        command, so pairing then waited for a message that never came and
        timed out.
        """
        import asyncio

        async def fake_updates(offset=None):
            if offset == -1:
                return {"ok": True, "updates": [{
                    "update_id": 700,
                    "message": {
                        "chat": {"id": 42, "type": "private"},
                        "from": {"first_name": "Connor"},
                        "text": "/start",
                    },
                }]}
            # Should never be reached: the backlog command pairs immediately.
            raise AssertionError("find_chat polled for a new update unnecessarily")

        self.notifier.get_updates = fake_updates
        found = asyncio.run(self.notifier.find_chat(timeout=5))
        self.assertTrue(found["ok"])
        self.assertEqual(found["chat"]["chat_id"], "42")
        self.assertEqual(found["paired_by"], "command")

    def test_non_command_backlog_does_not_pair_from_baseline(self):
        """Only a /start-style command pairs straight from the backlog."""
        import asyncio

        calls = []

        async def fake_updates(offset=None):
            calls.append(offset)
            if offset == -1:
                return {"ok": True, "updates": [{
                    "update_id": 800,
                    "message": {
                        "chat": {"id": 7, "type": "private"},
                        "from": {"first_name": "Someone"},
                        "text": "unrelated chatter",
                    },
                }]}
            # Never delivers anything new -> should time out, not pair on the
            # stale non-command message.
            return {"ok": True, "updates": []}

        self.notifier.get_updates = fake_updates
        found = asyncio.run(self.notifier.find_chat(timeout=0.2))
        self.assertFalse(found["ok"])
        # Resumed from after the baseline update.
        self.assertEqual(calls[0], -1)
        self.assertEqual(calls[1], 801)


    def test_get_updates_requires_token(self):
        import asyncio

        bare = TelegramNotifier(Settings(telegram_bot_token=""))
        result = asyncio.run(bare.get_updates())
        self.assertFalse(result["ok"])
        self.assertIn("No bot token", result["error"])


class TestTelegramErrorMessages(unittest.TestCase):
    """Telegram's bare 'Unauthorized' is translated into usable guidance."""

    def test_bad_token_is_explained(self):
        # This is exactly what Telegram returns for an invalid token.
        message = TelegramNotifier._explain_error(
            {"ok": False, "error_code": 401, "description": "Unauthorized"}, 401
        )
        self.assertIn("bot token", message.lower())
        # Guidance points at the dashboard flow now, not just .env.
        self.assertIn("BotFather", message)
        self.assertIn("Save token", message)
        # Must not just echo the opaque word back.
        self.assertNotEqual(message.strip(), "Unauthorized")

    def test_unauthorized_without_code_is_still_explained(self):
        message = TelegramNotifier._explain_error({"description": "Unauthorized"})
        self.assertIn("BotFather", message)

    def test_conflict_is_explained(self):
        message = TelegramNotifier._explain_error(
            {"error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
        )
        self.assertIn("conflict", message.lower())
        self.assertIn("getUpdates", message)

    def test_not_found_is_explained(self):
        message = TelegramNotifier._explain_error(
            {"error_code": 404, "description": "Not Found"}
        )
        self.assertIn("BotFather", message)

    def test_chat_not_found_gives_guidance(self):
        message = TelegramNotifier._explain_error(
            {"error_code": 400, "description": "chat not found"}
        )
        self.assertIn("/start", message)

    def test_unknown_error_is_preserved(self):
        message = TelegramNotifier._explain_error(
            {"error_code": 400, "description": "message is too long"}
        )
        self.assertIn("message is too long", message)

    def test_empty_error_has_a_fallback(self):
        self.assertIn("HTTP 500", TelegramNotifier._explain_error({}, 500))



if __name__ == "__main__":
    unittest.main(verbosity=2)
