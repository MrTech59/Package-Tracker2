"""Telegram notifications via the Bot API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings
from .models import Status

log = logging.getLogger(__name__)

_STATUS_ICONS = {
    Status.ORDERED: "🧾",
    Status.LABEL_CREATED: "🏷️",
    Status.SHIPPED: "📦",
    Status.IN_TRANSIT: "🚚",
    Status.OUT_FOR_DELIVERY: "🛵",
    Status.DELIVERED: "✅",
    Status.DELAYED: "⏳",
    Status.EXCEPTION: "⚠️",
    Status.RETURNED: "↩️",
    Status.CANCELLED: "🚫",
    Status.UNKNOWN: "❔",
}


def _escape(value: str) -> str:
    """Escape characters reserved by Telegram's HTML parse mode."""
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramNotifier:
    """Sends HTML-formatted messages to a configured chat."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.telegram_enabled
            and self.settings.telegram_bot_token
            and self.settings.telegram_chat_id
        )

    async def send_message(self, text: str) -> dict[str, Any]:
        """Send an arbitrary HTML message. Never raises."""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "Telegram is not configured"}

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds) as client:
                response = await client.post(url, json=payload)
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except ValueError:
                body = {"raw": response.text[:300]}

            if response.status_code >= 400 or not body.get("ok", False):
                log.warning("Telegram send failed (%s): %s", response.status_code, body)
                return {"ok": False, "status": response.status_code, "response": body}

            return {"ok": True, "message_id": body.get("result", {}).get("message_id")}
        except httpx.HTTPError as exc:
            log.warning("Telegram request error: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def notify_status_change(
        self,
        *,
        package_name: str,
        carrier_name: str,
        tracking_number: str,
        old_status: Status | None,
        new_status: Status,
        message: str = "",
        tracking_url: str = "",
    ) -> dict[str, Any]:
        """Send a formatted status-change notification."""
        icon = _STATUS_ICONS.get(new_status, "📦")
        lines = [f"{icon} <b>{_escape(package_name)}</b>"]

        if old_status is not None and old_status != new_status:
            lines.append(
                f"Status: {_escape(old_status.label)} → <b>{_escape(new_status.label)}</b>"
            )
        else:
            lines.append(f"Status: <b>{_escape(new_status.label)}</b>")

        if carrier_name:
            carrier_line = f"Carrier: {_escape(carrier_name)}"
            if tracking_number:
                carrier_line += f" · <code>{_escape(tracking_number)}</code>"
            lines.append(carrier_line)
        elif tracking_number:
            lines.append(f"Tracking: <code>{_escape(tracking_number)}</code>")

        if message:
            lines.append(f"<i>{_escape(message[:400])}</i>")

        if tracking_url:
            lines.append(f'<a href="{_escape(tracking_url)}">Track shipment</a>')

        return await self.send_message("\n".join(lines))

    async def check_connection(self) -> dict[str, Any]:
        """Verify the bot token and chat reachability via getMe + getChat."""
        if not self.settings.telegram_bot_token:
            return {"ok": False, "error": "No bot token configured"}
        base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
        try:
            async with httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds) as client:
                me = await client.get(f"{base}/getMe")
                me_body = me.json()
                if not me_body.get("ok"):
                    return {"ok": False, "error": me_body.get("description", "getMe failed")}

                result: dict[str, Any] = {
                    "ok": True,
                    "bot": me_body["result"].get("username"),
                }
                if self.settings.telegram_chat_id:
                    chat = await client.get(
                        f"{base}/getChat",
                        params={"chat_id": self.settings.telegram_chat_id},
                    )
                    chat_body = chat.json()
                    if chat_body.get("ok"):
                        result["chat"] = chat_body["result"].get("title") or chat_body["result"].get("username")
                    else:
                        result["ok"] = False
                        result["error"] = chat_body.get("description", "getChat failed")
                return result
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
