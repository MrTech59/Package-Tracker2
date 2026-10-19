"""Telegram notifications via the Bot API."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings
from .models import Status

log = logging.getLogger(__name__)

# Messages starting with one of these are treated as pairing commands rather
# than normal chatter, so a user can pair from any chat window.
PAIR_COMMANDS = ("/start", "/pair", "/subscribe")

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

    # ------------------------------------------------------------- pairing

    async def get_updates(self, offset: int | None = None) -> dict[str, Any]:
        """Long-poll getUpdates for recent messages.

        Returns ``{"ok": True, "updates": [...]}`` or ``{"ok": False, "error": ...}``.
        Never raises. `offset` acknowledges everything before it, so callers
        can replay only messages that arrived after a known point.
        """
        if not self.settings.telegram_bot_token:
            return {"ok": False, "error": "No bot token configured"}

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        params: dict[str, Any] = {
            "timeout": 0,
            "allowed_updates": '["message","channel_post"]',
        }
        if offset is not None:
            params["offset"] = offset
        try:
            async with httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds + 5) as client:
                response = await client.get(url, params=params)
            try:
                body = response.json()
            except ValueError:
                return {
                    "ok": False,
                    "error": f"Telegram returned an unreadable response ({response.status_code}).",
                }
            if not body.get("ok"):
                return {"ok": False, "error": self._explain_error(body, response.status_code)}
            return {"ok": True, "updates": body.get("result", [])}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"Could not reach Telegram: {exc}"}

    @staticmethod
    def _explain_error(body: dict[str, Any], status_code: int = 0) -> str:
        """Turn a Telegram error payload into something a human can act on.

        Telegram answers a bad token with a bare ``"Unauthorized"``, which is
        useless on its own, so those cases get explicit guidance.
        """
        description = str(body.get("description") or "").strip()
        error_code = body.get("error_code") or status_code
        lowered = description.lower()

        if error_code == 401 or "unauthorized" in lowered:
            return (
                "Telegram rejected the bot token (401 Unauthorized). "
                "Check TELEGRAM_BOT_TOKEN in your .env file -- it should look like "
                "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ."
            )
        if error_code == 409 or "conflict" in lowered:
            return (
                "Telegram reported a conflict (409). Another process is polling this "
                "bot with getUpdates. Stop it, or remove any webhook, then retry."
            )
        # Checked before the generic 404 rule below: "chat not found" is a 400
        # in practice but also contains the words "not found".
        if "chat not found" in lowered or "chat_id" in lowered:
            return (
                "Telegram could not find that chat. Send /start to your bot from "
                "the Telegram account you want to use, then pair again."
            )
        if error_code == 404:
            return (
                "Telegram could not find that bot (404). The token may have been "
                "revoked -- generate a new one with @BotFather."
            )
        if description:
            return f"Telegram error {error_code}: {description}"
        return f"Telegram request failed (HTTP {status_code})"

    @staticmethod
    def describe_update(update: dict[str, Any]) -> dict[str, Any] | None:
        """Extract the fields we care about from a raw Telegram update."""
        message = update.get("message") or update.get("channel_post") or update.get("edited_message")
        if not message:
            return None
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        sender = message.get("from") or {}
        name = (
            chat.get("title")
            or " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
            or sender.get("username")
            or chat.get("username")
            or str(chat_id)
        )
        return {
            "update_id": update.get("update_id"),
            "chat_id": str(chat_id),
            "chat_type": chat.get("type", "private"),
            "name": name,
            "username": sender.get("username") or chat.get("username") or "",
            "text": (message.get("text") or message.get("caption") or "").strip(),
            "date": message.get("date"),
        }

    @staticmethod
    def is_pair_command(text: str) -> bool:
        """True when a message looks like a pairing command (`/start` etc.)."""
        lowered = (text or "").strip().lower()
        return any(lowered == cmd or lowered.startswith(cmd + " ") for cmd in PAIR_COMMANDS)

    async def find_chat(self, *, replay: int = 30, timeout: float = 20.0) -> dict[str, Any]:
        """Wait for someone to message the bot, then hand back that chat.

        Used by the one-click pairing flow: we set the update offset *before*
        the user writes, so only genuinely new messages are considered. If a
        pairing command is seen it wins, otherwise the most recent chat is
        returned. Never raises.
        """
        if not self.settings.telegram_bot_token:
            return {"ok": False, "error": "No bot token configured. Add TELEGRAM_BOT_TOKEN first."}

        baseline = await self.get_updates(offset=-1)
        if not baseline.get("ok"):
            return baseline
        known = baseline.get("updates") or []

        # If the newest backlog entry is itself a pairing command, the user
        # almost certainly already sent /start and *then* pressed Pair. Using
        # it immediately is what they expect; skipping it in favour of a
        # message that may never come would just time out.
        for update in reversed(known):
            candidate = self.describe_update(update)
            if candidate and self.is_pair_command(candidate["text"]):
                log.info("Pairing with the existing /start from chat %s", candidate["chat_id"])
                return {"ok": True, "chat": candidate, "paired_by": "command"}

        offset = (known[-1]["update_id"] + 1) if known else 0

        deadline = asyncio.get_event_loop().time() + timeout
        fallback: dict[str, Any] | None = None

        while asyncio.get_event_loop().time() < deadline:
            polled = await self.get_updates(offset=offset)
            if not polled.get("ok"):
                return polled
            for update in polled.get("updates") or []:
                offset = max(offset, update["update_id"] + 1)
                candidate = self.describe_update(update)
                if candidate is None:
                    continue
                if self.is_pair_command(candidate["text"]):
                    return {"ok": True, "chat": candidate, "paired_by": "command"}
                fallback = candidate
            if fallback is not None:
                return {"ok": True, "chat": fallback, "paired_by": "message"}
            await asyncio.sleep(1.0)

        return {
            "ok": False,
            "error": (
                "No new message arrived. Open Telegram, send /start to your bot, "
                "then try pairing again."
            ),
        }

    async def send_to(self, chat_id: str, text: str) -> dict[str, Any]:
        """Send an HTML message to a specific chat, ignoring the configured one."""
        if not self.settings.telegram_bot_token:
            return {"ok": False, "error": "No bot token configured"}
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
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
                return {"ok": False, "status": response.status_code, "response": body}
            return {"ok": True, "message_id": body.get("result", {}).get("message_id")}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}

    async def send_pairing_confirmation(self, chat_id: str, bot_username: str = "") -> dict[str, Any]:
        """Greet a freshly paired chat and confirm alerts will arrive there."""
        handle = f"@{bot_username}" if bot_username else "this bot"
        text = (
            "✅ <b>Paired with Package Tracker</b>\n\n"
            f"This chat will now receive shipment status alerts from {_escape(handle)}.\n"
            "You will get a message whenever a tracked package changes status — "
            "shipped, out for delivery, delivered, delayed, and so on.\n\n"
            "<i>Sent from your self-hosted tracker.</i>"
        )
        return await self.send_to(chat_id, text)

