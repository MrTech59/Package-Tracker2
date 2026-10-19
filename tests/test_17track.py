"""Regression test using real captured 17track page text."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Status  # noqa: E402
from app.tracker.parser import parse_tracking_text  # noqa: E402

# Captured verbatim from the MCP browser reading a 17track results page.
# Note the legend counts: "In transit (1)" and "Delivered (0)".
SEVENTEEN_TRACK = """i
USPS tracking requests are limited due to policy changes. Follow our guide to unlock more requests.
Learn More>
×
All
(1)
Not found
(0)
Info received
(0)
In transit
(1)
Pick up
(0)
Out for Delivery
(0)
Undelivered
(0)
Delivered
(0)
Alert
(0)
Expired
(0)
TestNumber00017
In transit (5 Days)
China
China Post
United States
USPS
2022-03-25 19:57
CHINA,WUHAN, Processed Through Facility -> Your item has been processed through a facility in WUHAN, CHINA on March 25, 2022 at 8:57 am.
Estimated delivery time:
2022-04-05 - 2022-04-06
In transit
-
Estimated delivery time:
2022-04-05 - 2022-04-06
Your package has shipped from the originating country and is en route to its destination.
FAQ>
Tracking Information
Copy details
2022-03-25 14:30
WUHAN, Import Customs Clearance
2022-03-24 02:33
WUHAN, Arrived at [Wuhan International Mail Processing Center] (Transit)
2022-03-23 09:17
SHIYAN, Item posted at [Shiyan Urban District Delivery Division - International Center Branch]
"""

SEVENTEEN_TRACK_DELIVERED = """All (1)
Not found (0)
Info received (0)
In transit (0)
Pick up (0)
Out for Delivery (0)
Undelivered (0)
Delivered (1)
Alert (0)
Expired (0)
Order Number
Delivered
2022-03-25 19:57
Delivered to recipient
"""


class TestSeventeenTrack(unittest.TestCase):
    def test_legend_counts_do_not_override_status(self):
        """The "Delivered (0)" legend must not win over the real "In transit"."""
        result = parse_tracking_text(SEVENTEEN_TRACK)
        self.assertEqual(result.status, Status.IN_TRANSIT)
        self.assertTrue(result.confident)

    def test_expected_range(self):
        result = parse_tracking_text(SEVENTEEN_TRACK)
        self.assertEqual(result.expected_date, "2022-04-05")

    def test_real_delivered_page(self):
        result = parse_tracking_text(SEVENTEEN_TRACK_DELIVERED)
        self.assertEqual(result.status, Status.DELIVERED)

    def test_undelivered_is_exception_not_delivered(self):
        """A real 'Undelivered' status line beats the 'Delivered (0)' legend."""
        text = (
            "All (1)\nNot found (0)\nIn transit (0)\nUndelivered (0)\n"
            "Delivered (0)\nAlert (0)\nExpired (0)\n"
            "Order Number\nUndelivered\nDelivery was not successful\n"
        )
        result = parse_tracking_text(text)
        self.assertEqual(result.status, Status.EXCEPTION)

    def test_legend_only_undelivered_does_not_set_status(self):
        """A legend count alone (no status text) must not imply a status."""
        text = "All (1)\nUndelivered (1)\nDelivered (0)\nAlert (0)"
        result = parse_tracking_text(text)
        self.assertFalse(result.confident)
        self.assertEqual(result.status, Status.UNKNOWN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
