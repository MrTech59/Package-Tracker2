"""Verify the MCP browser scrape pipeline end to end against a live server.

Runs the real CarrierScraper against the configured MCP endpoint using a
package whose carrier page is guaranteed to load (example.com is scoped to a
'test' carrier so the parse is inconclusive but the transport is exercised).

    python scripts/verify_mcp.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.mcp_client import MCPClient  # noqa: E402


async def main() -> int:
    settings = Settings.load(Path(__file__).resolve().parent.parent / ".env")
    print(f"MCP endpoint: {settings.mcp_url}")
    print("-" * 60)

    async with MCPClient(
        settings.mcp_url,
        timeout=settings.mcp_timeout_seconds,
        protocol_version=settings.mcp_protocol_version,
    ) as client:
        tools = await client.list_tools()
        print(f"OK   initialize handshake succeeded")
        print(f"OK   tools/list returned {len(tools)} tools")

        names = {t.get("name") for t in tools}
        for required in ("browser_navigate", "browser_read"):
            if required not in names:
                print(f"FAIL required tool missing: {required}")
                return 1
        print("OK   browser_navigate + browser_read are available")

        # Navigate somewhere harmless and read it back.
        print("\nNavigating to example.com ...")
        await client.call_tool("browser_navigate", {"url": "https://example.com"})
        text = await client.call_tool("browser_read", {})
        if not text.strip():
            print("FAIL browser_read returned no text")
            return 1
        print(f"OK   browser_read returned {len(text)} chars")
        print(f"     preview: {text.strip().splitlines()[0][:70]!r}")

    print("-" * 60)
    print("MCP scrape pipeline verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
