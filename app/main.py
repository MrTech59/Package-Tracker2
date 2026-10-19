"""FastAPI application: REST API plus static frontend."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import settings
from .db import Database
from .models import CARRIERS, Status
from .service import TrackerService, init_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("package_tracker")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------- schemas


class PackageIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    tracking_number: str = Field(default="", max_length=200)
    carrier: str = Field(default="other", max_length=50)
    status: str = Field(default=Status.ORDERED.value)
    expected_date: str | None = None
    notes: str = Field(default="", max_length=2000)
    postal_code: str = Field(default="", max_length=20)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        try:
            return Status(value).value
        except ValueError as exc:
            raise ValueError(f"Unknown status: {value}") from exc

    @field_validator("expected_date")
    @classmethod
    def _valid_date(cls, value: str | None) -> str | None:
        if not value:
            return None
        import re

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("expected_date must be YYYY-MM-DD")
        return value


class PackagePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tracking_number: str | None = Field(default=None, max_length=200)
    carrier: str | None = Field(default=None, max_length=50)
    status: str | None = None
    expected_date: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    postal_code: str | None = Field(default=None, max_length=20)
    send_notification: bool = True

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return Status(value).value
        except ValueError as exc:
            raise ValueError(f"Unknown status: {value}") from exc


class StatusIn(BaseModel):
    status: str
    message: str = ""
    send_notification: bool = True

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        try:
            return Status(value).value
        except ValueError as exc:
            raise ValueError(f"Unknown status: {value}") from exc


class ImportIn(BaseModel):
    packages: list[PackageIn]
    replace: bool = False


# ------------------------------------------------------------- app wiring


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Optional shared-secret auth, enabled only when API_KEY is set."""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key header")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    db = Database(settings.db_path)
    service = init_service(settings, db)
    app.state.db = db
    app.state.service = service

    # A chat paired through the dashboard is stored in the database so it
    # survives restarts without the operator touching .env.
    paired = service.load_paired_chat()
    if paired:
        log.info("Telegram paired chat loaded from database: %s", paired)

    poll_task: asyncio.Task | None = None
    if settings.scrape_enabled and settings.poll_interval_seconds > 0:
        poll_task = asyncio.create_task(service.poll_loop())

    log.info("Package Tracker %s ready on %s:%s", __version__, settings.host, settings.port)
    log.info("MCP endpoint: %s | Telegram: %s", settings.mcp_url,
             "enabled" if service.notifier.enabled else "disabled")
    try:
        yield
    finally:
        if poll_task:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Package Tracker", version=__version__, lifespan=lifespan)


def svc() -> TrackerService:
    return app.state.service


# ------------------------------------------------------------------- routes


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "scrape_enabled": settings.scrape_enabled,
        "telegram_enabled": svc().notifier.enabled,
        "mcp_url": settings.mcp_url,
        "auth_required": bool(settings.api_key),
    }


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "carriers": [c.as_dict() for c in CARRIERS.values()],
        "statuses": [{"value": s.value, "label": s.label} for s in Status],
        "scrape_enabled": settings.scrape_enabled,
        "telegram_enabled": svc().notifier.enabled,
        "mcp_url": settings.mcp_url,
        "poll_interval_seconds": settings.poll_interval_seconds,
    }


@app.get("/api/packages", dependencies=[Depends(require_api_key)])
async def list_packages(
    q: str | None = Query(default=None, description="Search across name, tracking number, notes"),
    status: str | None = Query(default=None, description="Filter by status"),
    carrier: str | None = Query(default=None, description="Filter by carrier id"),
) -> dict[str, Any]:
    packages = svc().list_packages()

    if q:
        needle = q.strip().lower()
        packages = [
            p
            for p in packages
            if needle in (p.get("name") or "").lower()
            or needle in (p.get("tracking_number") or "").lower()
            or needle in (p.get("notes") or "").lower()
            or needle in (p.get("carrier_name") or "").lower()
        ]
    if status and status != "all":
        packages = [p for p in packages if p["status"] == status]
    if carrier and carrier != "all":
        packages = [p for p in packages if p["carrier_id"] == carrier]

    return {"packages": packages, "count": len(packages)}


@app.get("/api/packages/{package_id}", dependencies=[Depends(require_api_key)])
async def get_package(package_id: int) -> dict[str, Any]:
    package = svc().get_package(package_id)
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    return {"package": package}


@app.post("/api/packages", status_code=201, dependencies=[Depends(require_api_key)])
async def create_package(payload: PackageIn) -> dict[str, Any]:
    package = svc().create_package(payload.model_dump())
    return {"package": package}


@app.patch("/api/packages/{package_id}", dependencies=[Depends(require_api_key)])
async def update_package(package_id: int, payload: PackagePatch) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    send_notification = bool(data.pop("send_notification", True))

    existing = svc().get_package(package_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Package not found")

    new_status_value = data.get("status")
    status_changed = bool(new_status_value) and new_status_value != existing["status"]

    # Apply non-status fields first, then route the status through set_status
    # so events and notifications stay consistent.
    fields = {k: v for k, v in data.items() if k != "status"}
    if fields:
        svc().update_package(package_id, fields)

    old_status = Status(existing["status"])
    if status_changed:
        package, meta = svc().set_status(
            package_id, Status(new_status_value), source="manual"
        )
        if send_notification and package and svc().notifier.enabled:
            await svc().notify_status_change(package, old_status)
    else:
        package = svc().get_package(package_id)

    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    return {"package": package}


@app.delete("/api/packages/{package_id}", dependencies=[Depends(require_api_key)])
async def delete_package(package_id: int) -> dict[str, Any]:
    if not svc().delete_package(package_id):
        raise HTTPException(status_code=404, detail="Package not found")
    return {"ok": True}


@app.post("/api/packages/{package_id}/status", dependencies=[Depends(require_api_key)])
async def set_status(package_id: int, payload: StatusIn) -> dict[str, Any]:
    existing = svc().get_package(package_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Package not found")

    old_status = Status(existing["status"])
    package, _meta = svc().set_status(
        package_id,
        Status(payload.status),
        message=payload.message,
        source="manual",
    )
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")

    notified = False
    if payload.send_notification and svc().notifier.enabled and old_status.value != payload.status:
        result = await svc().notify_status_change(package, old_status, message=payload.message)
        notified = bool(result.get("ok"))

    return {"package": package, "notified": notified}


@app.get("/api/packages/{package_id}/events", dependencies=[Depends(require_api_key)])
async def package_events(package_id: int, limit: int = Query(default=50, ge=1, le=500)):
    if not svc().get_package(package_id):
        raise HTTPException(status_code=404, detail="Package not found")
    return {"events": svc().events_for(package_id, limit)}


@app.post("/api/packages/{package_id}/refresh", dependencies=[Depends(require_api_key)])
async def refresh_package(
    package_id: int,
    force: bool = Query(default=True),
    notify: bool = Query(default=True),
) -> dict[str, Any]:
    if not svc().get_package(package_id):
        raise HTTPException(status_code=404, detail="Package not found")
    return await svc().refresh_package(package_id, force=force, notify=notify)


@app.post("/api/refresh", dependencies=[Depends(require_api_key)])
async def refresh_all(force: bool = Query(default=True)) -> dict[str, Any]:
    return await svc().refresh_all(force=force, notify=True)


@app.get("/api/stats", dependencies=[Depends(require_api_key)])
async def stats() -> dict[str, Any]:
    return svc().stats()


@app.get("/api/events", dependencies=[Depends(require_api_key)])
async def recent_events(limit: int = Query(default=25, ge=1, le=200)):
    events = svc().db.recent_events(limit)
    for event in events:
        try:
            event["status_label"] = Status(event["status"]).label
        except ValueError:
            event["status_label"] = event["status"]
    return {"events": events}


# --------------------------------------------------------------- diagnostics


@app.get("/api/diagnostics/mcp", dependencies=[Depends(require_api_key)])
async def mcp_diagnostics() -> dict[str, Any]:
    return await svc().scraper.check_connection()


@app.get("/api/diagnostics/telegram", dependencies=[Depends(require_api_key)])
async def telegram_diagnostics() -> dict[str, Any]:
    return await svc().notifier.check_connection()


@app.post("/api/diagnostics/telegram/test", dependencies=[Depends(require_api_key)])
async def telegram_test() -> dict[str, Any]:
    if not svc().notifier.enabled:
        raise HTTPException(status_code=400, detail="Telegram is not configured")
    result = await svc().notifier.send_message(
        "🔔 <b>Package Tracker</b>\nTest notification — your Telegram alerts are working."
    )
    return result


# ----------------------------------------------------------- telegram pairing


class PairIn(BaseModel):
    chat_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)


@app.get("/api/telegram/pair", dependencies=[Depends(require_api_key)])
async def telegram_pair_status() -> dict[str, Any]:
    """Current pairing state, including whether a bot token is set."""
    return svc().pairing_status()


@app.post("/api/telegram/pair/auto", dependencies=[Depends(require_api_key)])
async def telegram_pair_auto(
    timeout: float = Query(default=20.0, ge=3, le=55),
) -> dict[str, Any]:
    """Wait for the user to message the bot, then pair with that chat."""
    result = await svc().auto_pair(timeout=timeout)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Pairing failed"))
    return result


@app.post("/api/telegram/pair", dependencies=[Depends(require_api_key)])
async def telegram_pair_manual(payload: PairIn) -> dict[str, Any]:
    """Pair with a chat id supplied directly (paste from @userinfobot etc.)."""
    try:
        pairing = svc().apply_pairing(payload.chat_id, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "pairing": pairing}


@app.delete("/api/telegram/pair", dependencies=[Depends(require_api_key)])
async def telegram_unpair() -> dict[str, Any]:
    """Forget the paired chat."""
    svc().unpair()
    return {"ok": True, "pairing": svc().pairing_status()}



# ------------------------------------------------------------ import / export


@app.get("/api/export", dependencies=[Depends(require_api_key)])
async def export_packages() -> JSONResponse:
    packages = svc().list_packages()
    exportable = [
        {
            k: p.get(k)
            for k in ("name", "tracking_number", "carrier_id", "status",
                      "expected_date", "notes", "postal_code", "created_at", "updated_at",
                      "delivered_at")
        }
        for p in packages
    ]
    return JSONResponse(
        content={"version": __version__, "exported_at": _now(), "packages": exportable},
        headers={"Content-Disposition": 'attachment; filename="packages.json"'},
    )


@app.post("/api/import", dependencies=[Depends(require_api_key)])
async def import_packages(payload: ImportIn) -> dict[str, Any]:
    if payload.replace:
        for existing in svc().db.list_packages():
            svc().delete_package(existing["id"])

    created = 0
    for item in payload.packages:
        svc().create_package(item.model_dump())
        created += 1
    return {"ok": True, "imported": created, "replaced": payload.replace}


@app.post("/api/import/raw", dependencies=[Depends(require_api_key)])
async def import_raw(payload: dict = Body(...)) -> dict[str, Any]:
    """Accept a previously exported file, tolerating extra fields."""
    packages = payload.get("packages")
    if not isinstance(packages, list):
        raise HTTPException(status_code=400, detail="Expected a 'packages' array")

    if payload.get("replace"):
        for existing in svc().db.list_packages():
            svc().delete_package(existing["id"])

    created = 0
    skipped: list[str] = []
    for raw in packages:
        if not isinstance(raw, dict):
            skipped.append("non-object entry")
            continue
        name = (raw.get("name") or "").strip()
        if not name:
            skipped.append("entry without a name")
            continue
        svc().create_package(
            {
                "name": name,
                "tracking_number": raw.get("tracking_number") or "",
                # Exports use carrier_id; API payloads use carrier.
                "carrier": raw.get("carrier_id") or raw.get("carrier") or "other",
                "status": raw.get("status") or Status.ORDERED.value,
                "expected_date": raw.get("expected_date") or None,
                "notes": raw.get("notes") or "",
                "postal_code": raw.get("postal_code") or "",
            }
        )
        created += 1

    return {
        "ok": True,
        "imported": created,
        "skipped": skipped,
        "replaced": bool(payload.get("replace")),
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------- static site


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")
