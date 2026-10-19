"""Core domain model: statuses, carriers, tracking URLs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    """Lifecycle of a shipment, ordered roughly by progression."""

    ORDERED = "ordered"
    LABEL_CREATED = "label_created"
    SHIPPED = "shipped"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELAYED = "delayed"
    EXCEPTION = "exception"
    RETURNED = "returned"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return LABELS[self]

    @property
    def is_terminal(self) -> bool:
        return self in {Status.DELIVERED, Status.RETURNED, Status.CANCELLED}

    @property
    def is_problem(self) -> bool:
        return self in {Status.DELAYED, Status.EXCEPTION}


LABELS: dict[Status, str] = {
    Status.ORDERED: "Ordered",
    Status.LABEL_CREATED: "Label created",
    Status.SHIPPED: "Shipped",
    Status.IN_TRANSIT: "In transit",
    Status.OUT_FOR_DELIVERY: "Out for delivery",
    Status.DELIVERED: "Delivered",
    Status.DELAYED: "Delayed",
    Status.EXCEPTION: "Exception",
    Status.RETURNED: "Returned",
    Status.CANCELLED: "Cancelled",
    Status.UNKNOWN: "Unknown",
}

# Progression order used to decide whether a status change is an advance or a regression.
_PROGRESSION: list[Status] = [
    Status.UNKNOWN,
    Status.ORDERED,
    Status.LABEL_CREATED,
    Status.SHIPPED,
    Status.IN_TRANSIT,
    Status.OUT_FOR_DELIVERY,
    Status.DELIVERED,
]


def progression_index(status: Status) -> int:
    """Rank a status within the happy path. Non-happy-path statuses rank as unknown."""
    try:
        return _PROGRESSION.index(status)
    except ValueError:
        return 0


def is_regression(old: Status, new: Status) -> bool:
    """True when `new` moves backwards along the happy path."""
    if new.is_problem or old.is_problem:
        return False
    return progression_index(new) < progression_index(old)


@dataclass(frozen=True)
class Carrier:
    """A shipping carrier with tracking URL templates."""

    id: str
    name: str
    # `{tracking}` is substituted with the tracking number.
    tracking_url: str
    # Optional carriers use a separate URL when a postal code is required.
    postal_url: str | None = None

    def url_for(self, tracking: str, postal_code: str | None = None) -> str:
        tracking = (tracking or "").strip()
        # URL-encode minimally: tracking numbers are alphanumeric with dashes.
        safe = re.sub(r"[^A-Za-z0-9\-]", "", tracking)
        if postal_code and self.postal_url:
            return self.postal_url.format(
                tracking=safe, postal=re.sub(r"[^A-Za-z0-9]", "", postal_code)
            )
        return self.tracking_url.format(tracking=safe)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name}


CARRIERS: dict[str, Carrier] = {
    c.id: c
    for c in [
        Carrier("usps", "USPS", "https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking}"),
        Carrier("ups", "UPS", "https://www.ups.com/track?tracknum={tracking}"),
        Carrier("fedex", "FedEx", "https://www.fedex.com/fedextrack/?trknbr={tracking}"),
        Carrier("dhl", "DHL", "https://www.dhl.com/en/express/tracking.html?AWB={tracking}"),
        Carrier("dhl_de", "DHL Germany", "https://www.dhl.de/en/privatkunden/pakete-empfangen/verfolgen.html?piececode={tracking}"),
        Carrier("royal_mail", "Royal Mail", "https://www.royalmail.com/track-your-item#/tracking-results/{tracking}"),
        Carrier(
            "canada_post",
            "Canada Post",
            "https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={tracking}",
        ),
        Carrier(
            "australia_post",
            "Australia Post",
            "https://auspost.com.au/mypost/track/#/details/{tracking}",
        ),
        Carrier(
            "amazon",
            "Amazon",
            "https://www.amazon.com/gp/your-account/order-history",
        ),
        Carrier(
            "on_trac",
            "OnTrac",
            "https://www.ontrac.com/tracking/?number={tracking}",
        ),
        Carrier("lasership", "LaserShip", "https://www.lasership.com/track/{tracking}"),
        Carrier(
            "purolator",
            "Purolator",
            "https://www.purolator.com/en/shipping/tracker?pin={tracking}",
        ),
        Carrier("dpd", "DPD", "https://tracking.dpd.de/status/en_US/parcel/{tracking}"),
        Carrier("gls", "GLS", "https://gls-group.eu/EU/en/parcel-tracking?match={tracking}"),
        Carrier("postnl", "PostNL", "https://postnl.nl/tracktrace/?B={tracking}"),
        # 17track renders results server-side, so the MCP browser can read it
        # even when a carrier does not. Used as the default scrape source.
        Carrier("17track", "17track", "https://t.17track.net/en#nums={tracking}"),
        Carrier("other", "Other", "https://t.17track.net/en#nums={tracking}"),
    ]
}

# Carriers whose own tracking page the MCP browser server can reliably read.
# Everything else should be scraped through the 17track aggregator.
SCRAPEABLE_DIRECTLY: frozenset[str] = frozenset(
    {"fedex", "royal_mail", "dhl", "dpd", "17track"}
)

# Aggregator used when a carrier page cannot be read directly.
AGGREGATOR_ID = "17track"

CARRIER_ALIASES: dict[str, str] = {
    "us postal service": "usps",
    "united states postal service": "usps",
    "united parcel service": "ups",
    "federal express": "fedex",
    "dhl express": "dhl",
    "deutsche post": "dhl_de",
    "canadapost": "canada_post",
    "auspost": "australia_post",
    "amazon logistics": "amazon",
    "laser ship": "lasership",
    "ontrac": "on_trac",
    "general logistics systems": "gls",
    "post nl": "postnl",
    "17track": "17track",
    "17track.net": "17track",
    "track17": "17track",
}


def resolve_carrier(value: str | None) -> Carrier:
    """Resolve a free-text carrier name to a known Carrier, falling back to `other`."""
    if not value:
        return CARRIERS["other"]
    key = value.strip().lower()
    if key in CARRIERS:
        return CARRIERS[key]
    if key in CARRIER_ALIASES:
        return CARRIERS[CARRIER_ALIASES[key]]
    # Substring match on id or name.
    for carrier in CARRIERS.values():
        if key in carrier.id or key in carrier.name.lower():
            return carrier
    return CARRIERS["other"]
