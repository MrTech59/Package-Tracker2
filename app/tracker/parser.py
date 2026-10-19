"""Parse carrier tracking-page text into a normalized status.

The MCP browser server returns *visible page text*. Carrier sites vary wildly,
so this uses ordered, weighted keyword rules with negative guards to avoid
matching navigation chrome ("Track another package", "Sign in", etc.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..models import Status


@dataclass
class ParsedTracking:
    """Result of interpreting a carrier page."""

    status: Status = Status.UNKNOWN
    summary: str = ""
    expected_date: str | None = None
    location: str | None = None
    matched_rule: str | None = None
    confident: bool = False
    warnings: list[str] = field(default_factory=list)


# Ordered rules: earlier entries win. Each rule is (regex, status, weight).
_RULES: list[tuple[str, Status, str]] = [
    (r"\bdelivered\b(?!\s*(?:to|by)\s+(?:us|carrier))", Status.DELIVERED, "delivered"),
    (r"\b(out for delivery|on vehicle for delivery|with delivery courier)\b", Status.OUT_FOR_DELIVERY, "out_for_delivery"),
    (r"\b(return(?:ed)? to sender|returning to sender|return to shipper)\b", Status.RETURNED, "returned"),
    # 17track labels: "Undelivered", "Alert", "Expired"
    (r"\b(exception|undelivered|delivery failed|failed attempt|delivery attempt was made|unable to deliver|held at|refused)\b", Status.EXCEPTION, "exception"),
    (r"\b(delayed|delay in delivery|shipment is delayed|weather delay)\b", Status.DELAYED, "delayed"),
    (r"\b(pick up|available for pickup|awaiting collection|ready for collection)\b", Status.OUT_FOR_DELIVERY, "pickup"),
    (r"\b(clearance event|customs clearance|being processed at customs|import customs cleanup)\b", Status.IN_TRANSIT, "customs"),
    (r"\b(in transit|en route|arrived at|departed|processed through facility|processed at|transit through|scanned|departed facility|arrived at facility|import customs|arrived at \[)\b", Status.IN_TRANSIT, "in_transit"),
    (r"\b(shipped|has left|accepted at|picked up|picked-up|origin scan|package received|item posted|handed over)\b", Status.SHIPPED, "shipped"),
    (r"\b(label created|shipping label|pre-shipment|awaiting item|electronic information|info received)\b", Status.LABEL_CREATED, "label_created"),
    (r"\b(order(?:ed)? (?:placed|confirmed)|order received)\b", Status.ORDERED, "ordered"),
    (r"\b(cancelled|canceled|order cancelled)\b", Status.CANCELLED, "cancelled"),
]

# 17track renders a status legend ("Delivered (0)", "In transit (1)") for every
# page. Counting those would always look "delivered", so strip only the legend
# lines that carry an explicit count — never bare status lines.
_LEGEND_LINE = re.compile(
    r"^\s*(?:all|not found|info received|in transit|pick up|out for delivery|"
    r"undelivered|delivered|alert|expired|pending)\s*\(\d+\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 17track may place the count on its own line, after the label.
_LEGEND_COUNT_ONLY = re.compile(r"^\s*\(\d+\)\s*$", re.MULTILINE)

# Text that indicates the page did not actually resolve a shipment.
_NO_RESULT_PATTERNS = [
    r"not found",
    r"no (?:results|information|tracking)",
    r"could not be found",
    r"invalid (?:tracking )?number",
    r"unable to locate",
    r"doesn'?t exist",
    r"enter (?:your )?tracking number",
    r"provide a tracking number",
]

_DATE_PATTERNS = [
    # "Wednesday, October 22, 2025"
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
    # "October 22, 2025" / "22 October 2025"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
    r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
    # ISO
    r"\b\d{4}-\d{2}-\d{2}\b",
    # US slash
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
]

_ETA_CONTEXT = re.compile(
    r"(?:estimated delivery|expected delivery|scheduled delivery|delivery date|arriving|arrives|expected|eta)[^.\n]{0,60}",
    re.IGNORECASE,
)

_LOCATION_CONTEXT = re.compile(
    r"(?:at|in|from|arrived at|departed)\s+([A-Z][A-Za-z.\- ]{2,40},?\s*(?:[A-Z]{2})?)\b"
)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}


def _parse_date_string(value: str) -> str | None:
    """Best-effort normalisation of a date string to ISO (YYYY-MM-DD)."""
    cleaned = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s*", "", value.strip(), flags=re.I)
    cleaned = re.sub(r"(st|nd|rd|th)\b", "", cleaned, flags=re.I).strip()

    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if iso_match:
        return cleaned

    slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if slash:
        month, day, year = (int(g) for g in slash.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    mdy = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", cleaned)
    if mdy:
        month = _MONTHS.get(mdy.group(1)[:3].lower())
        if month:
            try:
                return date(int(mdy.group(3)), month, int(mdy.group(2))).isoformat()
            except ValueError:
                return None

    dmy = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", cleaned)
    if dmy:
        month = _MONTHS.get(dmy.group(2)[:3].lower())
        if month:
            try:
                return date(int(dmy.group(3)), month, int(dmy.group(1))).isoformat()
            except ValueError:
                return None

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _looks_like_no_result(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _NO_RESULT_PATTERNS)


_LEGEND_LABELS = (
    "all|not found|info received|in transit|pick up|out for delivery|"
    "undelivered|delivered|alert|expired|pending"
)

# "Delivered (0)" — label and count on one line.
_LEGEND_INLINE = re.compile(
    rf"^[^\S\n]*(?:{_LEGEND_LABELS})[^\S\n]*\(\d+\)[^\S\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
# "Delivered\n(0)" — count alone on the following line.
_LEGEND_SPLIT = re.compile(
    rf"^[^\S\n]*(?:{_LEGEND_LABELS})[^\S\n]*\n[^\S\n]*\(\d+\)[^\S\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_legend(text: str) -> str:
    """Remove aggregator status-legend entries without touching real status text."""
    text = _LEGEND_SPLIT.sub("", text)
    text = _LEGEND_INLINE.sub("", text)
    return text


def _extract_expected_date(text: str) -> str | None:
    for context in _ETA_CONTEXT.finditer(text):
        window = context.group(0)
        for pattern in _DATE_PATTERNS:
            found = re.search(pattern, window)
            if found:
                parsed = _parse_date_string(found.group(0))
                if parsed:
                    return parsed
    return None


def _extract_expected_range(text: str) -> tuple[str | None, str | None]:
    """Extract a date range such as 17track's "2022-04-05 - 2022-04-06"."""
    match = re.search(
        r"estimated delivery time:?\s*(\d{4}-\d{2}-\d{2})\s*[-–]\s*(\d{4}-\d{2}-\d{2})",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2)
    match = re.search(
        r"estimated delivery time:?\s*(\d{4}-\d{2}-\d{2})", text, re.IGNORECASE
    )
    if match:
        return match.group(1), None
    return None, None


def _extract_location(text: str) -> str | None:
    match = _LOCATION_CONTEXT.search(text)
    if match:
        return match.group(1).strip(" ,")
    return None


def _clean_summary(line: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", line).strip()[:limit]


def parse_tracking_text(text: str, *, current: Status | None = None) -> ParsedTracking:
    """Interpret carrier page text.

    `current` is the package's existing status; when the scrape is inconclusive
    we keep it rather than regressing to `unknown`.
    """
    result = ParsedTracking()
    if not text or not text.strip():
        result.warnings.append("Carrier page was empty")
        if current is not None:
            result.status = current
        return result

    # Strip the 17track status legend first: its "Not found (0)" / "Delivered (0)"
    # entries would otherwise trip both the no-result guard and the matchers.
    text = _strip_legend(text)

    if _looks_like_no_result(text):
        result.warnings.append("Carrier page reported no tracking result")
        if current is not None:
            result.status = current
        return result

    lowered = text.lower()
    for pattern, status, rule_name in _RULES:
        match = re.search(pattern, lowered)
        if match:
            result.status = status
            result.matched_rule = rule_name
            result.confident = True
            result.summary = _find_context_line(text, match.start())
            break

    if not result.confident:
        result.warnings.append("No known status phrase found on carrier page")
        result.status = current if current is not None else Status.UNKNOWN
        result.summary = _clean_summary(text[:300])

    result.expected_date = _extract_expected_date(text)
    if not result.expected_date:
        start, end = _extract_expected_range(text)
        result.expected_date = start or end
    result.location = _extract_location(text)
    return result


def _find_context_line(text: str, index: int) -> str:
    """Return the line containing the matched phrase, trimmed for display."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    line = text[start:end]
    if len(line.strip()) < 8:
        # Fall back to a wider window when the match landed on a short fragment.
        line = text[max(0, index - 120): index + 120]
    return _clean_summary(line)
