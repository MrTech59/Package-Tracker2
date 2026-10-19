"""Tests for the carrier status parser."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Carrier, Status, is_regression, resolve_carrier  # noqa: E402
from app.tracker.parser import parse_tracking_text  # noqa: E402


class TestParser(unittest.TestCase):
    def test_delivered(self):
        text = "Tracking Number 1Z999AA10123456784\nDelivered\nLeft at front door\n"
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.DELIVERED)
        self.assertTrue(result.confident)

    def test_out_for_delivery(self):
        text = "Status: Out for delivery\nEstimated delivery October 22, 2025"
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.OUT_FOR_DELIVERY)
        self.assertEqual(result.expected_date, "2025-10-22")

    def test_in_transit_beats_label_created(self):
        text = "...Label created... \n\nArrived at facility\nIn transit to next facility"
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.IN_TRANSIT)

    def test_delayed(self):
        text = "Shipment is delayed due to weather"
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.DELAYED)

    def test_exception(self):
        text = "We're sorry. Your package had a delivery exception."
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.EXCEPTION)

    def test_returned(self):
        text = "Returned to sender"
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.RETURNED)

    def test_no_result_keeps_current(self):
        text = "We could not find any information for that tracking number."
        result = parse_tracking_text(text, current=Status.SHIPPED)
        self.assertEqual(result.status, Status.SHIPPED)
        self.assertFalse(result.confident)
        self.assertTrue(result.warnings)

    def test_empty_keeps_current(self):
        result = parse_tracking_text("", current=Status.IN_TRANSIT)
        self.assertEqual(result.status, Status.IN_TRANSIT)

    def test_inconclusive_unknown(self):
        result = parse_tracking_text("Welcome to our website. Sign in to continue.")
        self.assertEqual(result.status, Status.UNKNOWN)
        self.assertFalse(result.confident)

    def test_eta_iso(self):
        text = "In transit\nEstimated delivery 2025-11-03"
        result = parse_tracking_text(text)
        self.assertEqual(result.expected_date, "2025-11-03")

    def test_eta_dmy(self):
        text = "Arriving 22 October 2025"
        result = parse_tracking_text(text)
        self.assertEqual(result.expected_date, "2025-10-22")

    def test_shipped(self):
        result = parse_tracking_text("Your package has left the origin facility.")
        self.assertEqual(result.status, Status.SHIPPED)

    def test_label_created_only(self):
        result = parse_tracking_text("Shipping label created, USPS awaiting item")
        self.assertEqual(result.status, Status.LABEL_CREATED)


class TestCarriers(unittest.TestCase):
    def test_url_substitution(self):
        carrier = resolve_carrier("ups")
        self.assertIn("1Z999", carrier.url_for("1Z999"))

    def test_url_strips_unsafe(self):
        carrier = Carrier("test", "Test", "https://x.test/{tracking}")
        self.assertEqual(carrier.url_for("1Z-999  !!"), "https://x.test/1Z-999")

    def test_alias(self):
        self.assertEqual(resolve_carrier("United States Postal Service").id, "usps")

    def test_unknown_falls_back(self):
        self.assertEqual(resolve_carrier("Some Tiny Courier").id, "other")

    def test_none(self):
        self.assertEqual(resolve_carrier(None).id, "other")


class TestProgression(unittest.TestCase):
    def test_regression(self):
        self.assertTrue(is_regression(Status.DELIVERED, Status.IN_TRANSIT))

    def test_advance_not_regression(self):
        self.assertFalse(is_regression(Status.IN_TRANSIT, Status.DELIVERED))

    def test_problem_not_regression(self):
        self.assertFalse(is_regression(Status.IN_TRANSIT, Status.DELAYED))


if __name__ == "__main__":
    unittest.main(verbosity=2)
