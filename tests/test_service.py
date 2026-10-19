"""Tests for the database layer and service logic."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import Status  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
