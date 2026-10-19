"""Scrape carrier tracking pages through the MCP browser server."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..config import Settings
from ..mcp_client import MCPClient, MCPError
from ..models import AGGREGATOR_ID, SCRAPEABLE_DIRECTLY, Carrier, Status, resolve_carrier
from .parser import ParsedTracking, parse_tracking_text

log = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    """Outcome of a scraping attempt for one package."""

    ok: bool
    parsed: ParsedTracking | None = None
    url: str = ""
    error: str | None = None
    steps: list[str] = field(default_factory=list)


class CarrierScraper:
    """Drives the MCP browser tools to read a carrier tracking page."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def scrape(
        self, tracking_number: str, carrier_id: str, *, postal_code: str = ""
    ) -> ScrapeResult:
        tracking_number = (tracking_number or "").strip()
        if not tracking_number:
            return ScrapeResult(ok=False, error="No tracking number on this package")

        carrier: Carrier = resolve_carrier(carrier_id)

        # Try the carrier's own page first when the browser can read it, then
        # fall back to the 17track aggregator, which renders server-side.
        candidates: list[Carrier] = []
        if carrier.id in SCRAPEABLE_DIRECTLY and carrier.id != AGGREGATOR_ID:
            candidates.append(carrier)
        candidates.append(resolve_carrier(AGGREGATOR_ID))

        steps: list[str] = []
        try:
            async with MCPClient(
                self.settings.mcp_url,
                timeout=self.settings.mcp_timeout_seconds,
                protocol_version=self.settings.mcp_protocol_version,
            ) as client:
                available = await self._tool_names(client)
                if not available:
                    return ScrapeResult(ok=False, steps=steps,
                                        error="MCP server exposed no tools")

                best: ScrapeResult | None = None
                for candidate in candidates:
                    url = candidate.url_for(tracking_number, postal_code or None)
                    parsed = await self._scrape_url(client, url, steps)

                    result = ScrapeResult(ok=True, parsed=parsed, url=url, steps=list(steps))
                    if parsed.confident:
                        return result

                    # Keep the first attempt that at least produced page text.
                    if best is None or (not best.parsed and parsed is not None):
                        best = result
                    steps.append(f"inconclusive on {candidate.name}; trying next source")

                assert best is not None
                best.parsed.warnings.append("No source produced a confident status")
                return best

        except MCPError as exc:
            log.warning("Scrape failed for %s: %s", tracking_number, exc)
            return ScrapeResult(ok=False, error=str(exc), steps=steps)
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the UI
            log.exception("Unexpected scrape failure for %s", tracking_number)
            return ScrapeResult(ok=False, error=f"Unexpected error: {exc}", steps=steps)

    async def _scrape_url(self, client: MCPClient, url: str, steps: list[str]) -> ParsedTracking:
        """Navigate to one URL and parse the visible text."""
        await client.call_tool("browser_navigate", {"url": url})
        steps.append(f"navigated to {url}")

        # Give JS-heavy pages a moment, then wait for a status element if present.
        await self._best_effort(
            client, "browser_wait", {"seconds": 4}, steps, "waited 4s for render"
        )
        await self._best_effort(
            client, "browser_wait_for",
            {"selector": "[class*=status], [class*=track], [id*=track]", "timeout": 8000},
            steps, "waited for status element",
        )

        text, attempts = await self._read_with_retry(client, steps)
        if len(text) > self.settings.max_page_chars:
            text = text[: self.settings.max_page_chars]

        parsed = parse_tracking_text(text)
        if not text.strip():
            parsed.warnings.append(
                f"Page returned no readable text after {attempts} attempts"
            )
        return parsed

    async def _read_with_retry(
        self, client: MCPClient, steps: list[str], *, attempts: int = 3
    ) -> tuple[str, int]:
        """Read page text, retrying while the page is still rendering.

        Returns the best (longest) text seen and the number of attempts made.
        """
        best = ""
        for attempt in range(1, attempts + 1):
            text = await client.call_tool("browser_read", {})
            steps.append(f"read {len(text)} chars (attempt {attempt})")
            if len(text) > len(best):
                best = text
            # Accept anything substantial; otherwise wait and try again.
            if len(text.strip()) >= 120:
                break
            if attempt < attempts:
                await self._best_effort(
                    client, "browser_wait", {"seconds": 3}, steps,
                    f"waited for render ({attempt})",
                )
                await asyncio.sleep(1.5)
        return best, attempts

    async def _tool_names(self, client: MCPClient) -> set[str]:
        try:
            tools = await client.list_tools()
        except MCPError:
            return set()
        return {t.get("name", "") for t in tools}

    async def _best_effort(
        self,
        client: MCPClient,
        tool: str,
        arguments: dict,
        steps: list[str],
        note: str,
    ) -> None:
        """Run an optional tool; never fail the scrape because it is missing."""
        try:
            await client.call_tool(tool, arguments, retries=0)
            steps.append(note)
        except MCPError as exc:
            log.debug("Optional tool %s skipped: %s", tool, exc)

    # ----------------------------------------------------------- connectivity

    async def check_connection(self) -> dict:
        """Probe the MCP server; used by the UI's health indicator."""
        try:
            async with MCPClient(
                self.settings.mcp_url,
                timeout=min(self.settings.mcp_timeout_seconds, 15.0),
                protocol_version=self.settings.mcp_protocol_version,
            ) as client:
                tools = await client.list_tools()
            return {
                "ok": True,
                "url": self.settings.mcp_url,
                "tool_count": len(tools),
                "tools": [t.get("name") for t in tools],
            }
        except MCPError as exc:
            return {"ok": False, "url": self.settings.mcp_url, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": self.settings.mcp_url, "error": f"Unexpected error: {exc}"}


def apply_parse_to_status(parsed: ParsedTracking, current: Status) -> Status:
    """Decide the status to store, guarding against inconclusive regressions."""
    if not parsed.confident and parsed.status in (Status.UNKNOWN, current):
        return current
    return parsed.status
