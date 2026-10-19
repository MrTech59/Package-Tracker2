"""Business logic: package operations, status changes, notifications, polling."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .db import Database, utcnow
from .models import Status, resolve_carrier
from .notifier import TelegramNotifier
from .tracker import CarrierScraper, apply_parse_to_status
from .tracker.scraper import ScrapeResult

log = logging.getLogger(__name__)

# Statuses we keep polling; terminal states are left alone.
ACTIVE_STATUSES = {
    Status.ORDERED,
    Status.LABEL_CREATED,
    Status.SHIPPED,
    Status.IN_TRANSIT,
    Status.OUT_FOR_DELIVERY,
    Status.DELAYED,
    Status.EXCEPTION,
    Status.UNKNOWN,
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class TrackerService:
    """Coordinates the database, MCP scraper, and Telegram notifier."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.scraper = CarrierScraper(settings)
        self.notifier = TelegramNotifier(settings)
        self._scrape_locks: dict[int, asyncio.Lock] = {}
        # Chat id that came from configuration (.env / environment) rather than
        # from dashboard pairing. Unpairing restores this value. A non-empty
        # settings value wins; otherwise fall back to the environment.
        self._env_chat_id = (
            (settings.telegram_chat_id or "").strip()
            or (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        )
        # Whether the operator explicitly set TELEGRAM_ENABLED in the
        # environment / .env. When they did, we honour their choice and never
        # auto-enable notifications; when they did not, pairing turns them on.
        self._telegram_explicitly_configured = os.environ.get("TELEGRAM_ENABLED") is not None

    # ------------------------------------------------------------- read paths

    def decorate(self, package: dict[str, Any]) -> dict[str, Any]:
        """Add derived fields the UI needs (labels, tracking URL, flags)."""
        carrier = resolve_carrier(package.get("carrier"))
        status = Status(package.get("status") or Status.UNKNOWN)
        tracking_number = package.get("tracking_number") or ""
        enriched = dict(package)
        enriched.update(
            {
                "status_label": status.label,
                "status_is_terminal": status.is_terminal,
                "status_is_problem": status.is_problem,
                "carrier_name": carrier.name,
                "carrier_id": carrier.id,
                "tracking_url": carrier.url_for(tracking_number, package.get("postal_code") or None),
                "has_tracking": bool(tracking_number),
                "can_refresh": bool(tracking_number) and carrier.id != "other",
            }
        )
        return enriched

    def list_packages(self) -> list[dict[str, Any]]:
        return [self.decorate(p) for p in self.db.list_packages()]

    def get_package(self, package_id: int) -> dict[str, Any] | None:
        package = self.db.get_package(package_id)
        return self.decorate(package) if package else None

    def events_for(self, package_id: int, limit: int = 100) -> list[dict[str, Any]]:
        events = self.db.list_events(package_id, limit)
        for event in events:
            try:
                event["status_label"] = Status(event["status"]).label
            except ValueError:
                event["status_label"] = event["status"]
        return events

    def stats(self) -> dict[str, Any]:
        """Summary counts for the dashboard."""
        packages = self.db.list_packages()
        counts: dict[str, int] = {s.value: 0 for s in Status}
        for package in packages:
            counts[package["status"]] = counts.get(package["status"], 0) + 1

        in_flight = sum(
            counts[s.value]
            for s in (Status.ORDERED, Status.LABEL_CREATED, Status.SHIPPED,
                      Status.IN_TRANSIT, Status.OUT_FOR_DELIVERY)
        )
        problems = counts[Status.DELAYED.value] + counts[Status.EXCEPTION.value]
        upcoming = [
            p
            for p in packages
            if p.get("expected_date") and p["status"] not in
            (Status.DELIVERED.value, Status.CANCELLED.value, Status.RETURNED.value)
        ]
        upcoming.sort(key=lambda p: p["expected_date"])

        return {
            "total": len(packages),
            "in_flight": in_flight,
            "delivered": counts[Status.DELIVERED.value],
            "problems": problems,
            "out_for_delivery": counts[Status.OUT_FOR_DELIVERY.value],
            "counts": counts,
            "upcoming": [
                {"id": p["id"], "name": p["name"], "expected_date": p["expected_date"]}
                for p in upcoming[:5]
            ],
        }

    # ------------------------------------------------------------ write paths

    def create_package(self, data: dict[str, Any]) -> dict[str, Any]:
        carrier = resolve_carrier(data.get("carrier"))
        try:
            status = Status(data.get("status") or Status.ORDERED)
        except ValueError:
            status = Status.ORDERED

        package = self.db.create_package(
            name=(data.get("name") or "Untitled package"),
            tracking_number=data.get("tracking_number") or "",
            carrier=carrier.id,
            status=status,
            expected_date=data.get("expected_date") or None,
            notes=data.get("notes") or "",
            postal_code=data.get("postal_code") or "",
        )
        return self.decorate(package)

    def update_package(self, package_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.db.get_package(package_id)
        if not existing:
            return None

        fields: dict[str, Any] = {}
        for key in ("name", "tracking_number", "notes", "postal_code", "expected_date"):
            if key in data and data[key] is not None:
                fields[key] = data[key]
        if "carrier" in data and data["carrier"]:
            fields["carrier"] = resolve_carrier(data["carrier"]).id
        if "status" in data and data["status"]:
            fields["status"] = data["status"]

        updated = self.db.update_package(package_id, **fields)
        if not updated:
            return None
        return self.decorate(updated)

    def delete_package(self, package_id: int) -> bool:
        return self.db.delete_package(package_id)

    # -------------------------------------------------------- status pipeline

    def set_status(
        self,
        package_id: int,
        new_status: Status,
        *,
        message: str = "",
        source: str = "manual",
        notify: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Record a status change, logging an event and queueing a notification.

        Returns (package, notification_result). The caller sends the
        notification asynchronously; this method is sync for easy use in
        request handlers.
        """
        existing = self.db.get_package(package_id)
        if not existing:
            return None, None

        old_status = Status(existing["status"]) if existing.get("status") else Status.UNKNOWN
        fields: dict[str, Any] = {"status": new_status.value}
        if new_status == Status.DELIVERED and not existing.get("delivered_at"):
            fields["delivered_at"] = utcnow()
        if new_status != Status.DELIVERED:
            fields["delivered_at"] = None

        updated = self.db.update_package(package_id, **fields)
        if not updated:
            return None, None

        if old_status != new_status or message:
            self.db.add_event(package_id, new_status, message, source)

        decorated = self.decorate(updated)
        return decorated, {"changed": old_status != new_status, "old_status": old_status}

    async def notify_status_change(
        self,
        package: dict[str, Any],
        old_status: Status | None,
        *,
        message: str = "",
    ) -> dict[str, Any]:
        new_status = Status(package["status"])
        return await self.notifier.notify_status_change(
            package_name=package["name"],
            carrier_name=package.get("carrier_name", ""),
            tracking_number=package.get("tracking_number", ""),
            old_status=old_status,
            new_status=new_status,
            message=message,
            tracking_url=package.get("tracking_url", ""),
        )

    # --------------------------------------------------------------- pairing

    TELEGRAM_CHAT_META_KEY = "telegram_chat_id"

    def _saved_chat_id(self) -> str:
        return (self.db.get_meta(self.TELEGRAM_CHAT_META_KEY) or "").strip()

    def telegram_chat_id(self) -> str:
        """The active chat id: paired-at-runtime value wins over .env."""
        return self._saved_chat_id() or self._configured_chat_id()

    def _configured_chat_id(self) -> str:
        """Chat id from configuration (.env / environment). Never the paired one."""
        return self._env_chat_id

    def load_paired_chat(self) -> str | None:
        """Apply any persisted pairing to the running notifier.

        Called on startup so a chat paired through the dashboard survives a
        restart without editing .env.
        """
        saved = self._saved_chat_id()
        if not saved:
            return None
        self.notifier.settings.telegram_chat_id = saved
        # Pairing implies the user wants alerts. Preserve an explicit
        # TELEGRAM_ENABLED=false from .env, but otherwise make sure the live
        # notifier is actually on -- otherwise a paired chat would silently
        # stop receiving messages after a restart.
        if not self.notifier.settings.telegram_enabled and not self._telegram_explicitly_configured:
            self.notifier.settings.telegram_enabled = True
        return saved

    def apply_pairing(self, chat_id: str, name: str = "", username: str = "") -> dict[str, Any]:
        """Persist a chat id and switch the live notifier over to it."""
        chat_id = str(chat_id).strip()
        if not chat_id:
            raise ValueError("chat_id is required")

        self.db.set_meta(self.TELEGRAM_CHAT_META_KEY, chat_id)
        self.db.set_meta(f"{self.TELEGRAM_CHAT_META_KEY}_name", name)
        # Only the notifier's working copy changes; settings stay as configured.
        self.notifier.settings.telegram_chat_id = chat_id
        # Pairing implies the user wants alerts, so turn them on unless the
        # operator explicitly disabled Telegram in .env.
        if not self.notifier.settings.telegram_enabled and not self._telegram_explicitly_configured:
            self.notifier.settings.telegram_enabled = True

        log.info("Telegram paired with chat %s (%s)", chat_id, name or "unnamed")
        return {
            "chat_id": chat_id,
            "name": name,
            "username": username,
            "notifications_enabled": self.notifier.enabled,
        }

    def unpair(self) -> None:
        """Forget the paired chat (falls back to TELEGRAM_CHAT_ID from .env)."""
        self.db.set_meta(self.TELEGRAM_CHAT_META_KEY, "")
        self.db.set_meta(f"{self.TELEGRAM_CHAT_META_KEY}_name", "")
        # Drop back to whatever configuration provided.
        self.notifier.settings.telegram_chat_id = self._configured_chat_id()

    def pairing_status(self) -> dict[str, Any]:
        """Snapshot of how Telegram is wired up right now."""
        saved = self._saved_chat_id()
        chat_id = self.telegram_chat_id()
        configured = self._configured_chat_id()
        return {
            "has_token": bool(self.settings.telegram_bot_token),
            "chat_id": chat_id,
            "chat_name": (self.db.get_meta(f"{self.TELEGRAM_CHAT_META_KEY}_name") or ""),
            "paired": bool(chat_id),
            "paired_via_dashboard": bool(saved),
            "paired_from_env": bool(not saved and configured),
            "notifications_enabled": self.notifier.enabled,
            "enabled_flag": bool(self.settings.telegram_enabled),
        }

    async def auto_pair(self, timeout: float = 20.0) -> dict[str, Any]:
        """Wait for a Telegram message and pair with the chat that sent it."""
        if not self.settings.telegram_bot_token:
            return {"ok": False, "error": "Add TELEGRAM_BOT_TOKEN first."}

        found = await self.notifier.find_chat(timeout=timeout)
        if not found.get("ok"):
            return found

        chat = found["chat"]
        paired = self.apply_pairing(chat["chat_id"], chat.get("name", ""), chat.get("username", ""))

        bot_username = ""
        probe = await self.notifier.check_connection()
        if probe.get("ok"):
            bot_username = probe.get("bot") or ""
        confirmation = await self.notifier.send_pairing_confirmation(chat["chat_id"], bot_username)

        return {
            "ok": True,
            "chat": chat,
            "pairing": paired,
            "confirmation_sent": bool(confirmation.get("ok")),
            "confirmation_error": confirmation.get("error") if not confirmation.get("ok") else None,
        }

    # ------------------------------------------------------------- refreshing

    def _lock_for(self, package_id: int) -> asyncio.Lock:
        lock = self._scrape_locks.get(package_id)
        if lock is None:
            lock = asyncio.Lock()
            self._scrape_locks[package_id] = lock
        return lock

    def _too_soon(self, package: dict[str, Any]) -> bool:
        last = _parse_iso(package.get("last_checked_at"))
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < self.settings.min_scrape_gap_seconds

    async def refresh_package(
        self, package_id: int, *, force: bool = False, notify: bool = True
    ) -> dict[str, Any]:
        """Scrape one package and apply any status change."""
        package = self.db.get_package(package_id)
        if not package:
            return {"ok": False, "error": "Package not found"}

        decorated = self.decorate(package)

        if not self.settings.scrape_enabled:
            return {"ok": False, "error": "Scraping is disabled (SCRAPE_ENABLED=false)",
                    "package": decorated}
        if not package.get("tracking_number"):
            return {"ok": False, "error": "This package has no tracking number",
                    "package": decorated}
        if not force and self._too_soon(package):
            return {"ok": False, "error": "Checked too recently; try again shortly",
                    "package": decorated}

        async with self._lock_for(package_id):
            result: ScrapeResult = await self.scraper.scrape(
                package["tracking_number"],
                package.get("carrier") or "other",
                postal_code=package.get("postal_code") or "",
            )

        if not result.ok or result.parsed is None:
            self.db.update_package(
                package_id,
                last_checked_at=utcnow(),
                last_error=result.error or "Unknown scraping error",
            )
            return {
                "ok": False,
                "error": result.error or "Scrape failed",
                "url": result.url,
                "steps": result.steps,
                "package": self.get_package(package_id),
            }

        current = Status(package.get("status") or Status.UNKNOWN)
        parsed = result.parsed
        target = apply_parse_to_status(parsed, current)

        update_fields: dict[str, Any] = {
            "last_checked_at": utcnow(),
            "last_error": None,
        }
        if parsed.expected_date:
            update_fields["expected_date"] = parsed.expected_date

        self.db.update_package(package_id, **update_fields)

        changed = target != current
        notification: dict[str, Any] | None = None

        if changed:
            updated, meta = self.set_status(
                package_id,
                target,
                message=parsed.summary or "Status updated from carrier page",
                source="scrape",
                notify=False,
            )
            if notify and self.notifier.enabled and updated:
                notification = await self.notify_status_change(
                    updated,
                    meta["old_status"] if meta else None,
                    message=parsed.summary,
                )
        else:
            # Record the scrape as a non-status-change event for the timeline.
            self.db.add_event(
                package_id,
                target,
                parsed.summary or "Checked carrier page; no change",
                "scrape",
            )

        final = self.get_package(package_id)
        return {
            "ok": True,
            "changed": changed,
            "status": target.value,
            "previous_status": current.value,
            "summary": parsed.summary,
            "confident": parsed.confident,
            "warnings": parsed.warnings,
            "expected_date": parsed.expected_date,
            "url": result.url,
            "steps": result.steps,
            "notified": bool(notification and notification.get("ok")),
            "package": final,
        }

    async def refresh_all(self, *, force: bool = True, notify: bool = True) -> dict[str, Any]:
        """Refresh every active package that has a tracking number."""
        candidates = [
            p
            for p in self.db.list_packages()
            if p.get("tracking_number")
            and p.get("status") in {s.value for s in ACTIVE_STATUSES}
        ]
        results = []
        for package in candidates:
            outcome = await self.refresh_package(package["id"], force=force, notify=notify)
            results.append({"id": package["id"], "name": package["name"], **{
                k: outcome.get(k) for k in ("ok", "changed", "status", "error")
            }})
            # Be polite to carrier sites.
            await asyncio.sleep(1.0)
        return {
            "checked": len(results),
            "changed": sum(1 for r in results if r.get("changed")),
            "results": results,
        }

    # ---------------------------------------------------------------- polling

    async def poll_loop(self) -> None:
        """Background loop refreshing active packages on an interval."""
        interval = max(60, self.settings.poll_interval_seconds)
        log.info("Polling loop started (every %ss)", interval)
        # Let the server finish starting up before the first pass.
        await asyncio.sleep(10)
        while True:
            try:
                if self.settings.scrape_enabled:
                    summary = await self.refresh_all(force=False, notify=True)
                    log.info(
                        "Poll complete: %s checked, %s changed",
                        summary["checked"], summary["changed"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad poll must not kill the loop
                log.exception("Polling loop error")
            await asyncio.sleep(interval)


_service: TrackerService | None = None


def init_service(settings: Settings, db: Database) -> TrackerService:
    global _service
    _service = TrackerService(settings, db)
    return _service


def get_service() -> TrackerService:
    if _service is None:
        raise RuntimeError("TrackerService has not been initialised")
    return _service
