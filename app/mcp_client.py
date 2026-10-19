"""Minimal MCP (Model Context Protocol) streamable-HTTP client.

Implements just enough of the protocol to talk to a `picoclaw-browser`-style
server: initialize handshake, session-id propagation, and tools/call.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when the MCP server returns an error or is unreachable."""


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse an SSE stream body into a list of JSON messages."""
    messages: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            log.debug("Skipping non-JSON SSE payload: %.200s", payload)
            continue
        if isinstance(parsed, list):
            messages.extend(parsed)
        else:
            messages.append(parsed)
    return messages


class MCPClient:
    """A short-lived MCP session against a streamable-HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 60.0,
        protocol_version: str = "2024-11-05",
    ):
        self.url = url
        self.timeout = timeout
        self.protocol_version = protocol_version
        self._session_id: str | None = None
        self._request_id = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    # ------------------------------------------------------------ HTTP plumbing

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPError(f"Cannot reach MCP server at {self.url}: {exc}") from exc

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        response = await self._post(payload)
        if response.status_code >= 400:
            raise MCPError(
                f"MCP {method} failed with HTTP {response.status_code}: {response.text[:300]}"
            )

        # Capture the session id the server hands back.
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages = _parse_sse(response.text)
        else:
            try:
                messages = [response.json()]
            except json.JSONDecodeError as exc:
                raise MCPError(f"MCP returned unparseable body: {response.text[:300]}") from exc

        for message in messages:
            if message.get("id") == payload["id"] or "result" in message or "error" in message:
                if "error" in message:
                    err = message["error"]
                    raise MCPError(f"MCP {method} error: {err.get('message', err)}")
                if "result" in message:
                    return message["result"]
        raise MCPError(f"MCP {method} returned no result: {response.text[:300]}")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            await self._post(payload)
        except MCPError:
            # Notifications are best-effort.
            log.debug("Notification %s failed", method, exc_info=True)

    # ------------------------------------------------------------ public API

    async def __aenter__(self) -> "MCPClient":
        await self.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "package-tracker", "version": "1.0.0"},
            },
        )
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list")
        return result.get("tools", []) or []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, retries: int = 2
    ) -> str:
        """Call a tool and return its text output.

        Retries once or twice on transient transport errors, since the browser
        server may be busy driving a page.
        """
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                result = await self._request(
                    "tools/call", {"name": name, "arguments": arguments or {}}
                )
                return self._extract_text(result)
            except MCPError as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last_error or MCPError(f"Tool {name} failed")

    @staticmethod
    def _extract_text(result: dict[str, Any]) -> str:
        """Flatten an MCP tool result into plain text."""
        if result.get("isError"):
            raise MCPError(f"Tool reported an error: {MCPClient._content_text(result)[:300]}")
        return MCPClient._content_text(result)

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        content = result.get("content") or []
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "resource":
                resource = item.get("resource") or {}
                parts.append(resource.get("text") or resource.get("uri", ""))
            else:
                parts.append(json.dumps(item))
        if not parts and "structuredContent" in result:
            parts.append(json.dumps(result["structuredContent"]))
        return "\n".join(p for p in parts if p)

    async def close(self) -> None:
        await self._client.aclose()
