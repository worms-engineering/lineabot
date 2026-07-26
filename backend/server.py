"""Tennis Value-Bet Monitor - FastAPI server."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from monitor import TennisMonitor

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("tennis-monitor")

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "10"))  # 6 refresh / ora

mongo_url = os.environ["MONGO_URL"]
db_name = os.environ["DB_NAME"]
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]

monitor = TennisMonitor(db)
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await monitor.load_settings()
    # REFRESH_MINUTES <= 0 disables the automatic scheduler: scans then happen
    # only when triggered (e.g. the frontend polls /api/refresh while open), so
    # no OddsPapi calls are burned when nobody is watching.
    if REFRESH_MINUTES > 0:
        scheduler.add_job(
            monitor.scan_once,
            trigger=IntervalTrigger(minutes=REFRESH_MINUTES),
            id="tennis-scan",
            next_run_time=datetime.now(timezone.utc),  # run immediately at startup
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("Scheduler started - scan every %d minutes", REFRESH_MINUTES)
    else:
        logger.info("Automatic scheduler disabled (REFRESH_MINUTES=%s) - scans are on-demand only", REFRESH_MINUTES)
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await monitor.close()
        client.close()


app = FastAPI(title="Tennis Value Monitor", lifespan=lifespan)
api = APIRouter(prefix="/api")


# ---- Models ----------------------------------------------------------------

class SettingsIn(BaseModel):
    drop_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    football_drop_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    tracking_enabled: bool | None = None
    basketball_enabled: bool | None = None
    football_enabled: bool | None = None
    provider: str | None = None
    football_provider: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class SettingsOut(BaseModel):
    drop_threshold: float
    football_drop_threshold: float
    tracking_enabled: bool
    basketball_enabled: bool
    football_enabled: bool
    provider: str
    football_provider: str
    providers: list[str]
    telegram_configured: bool
    refresh_minutes: int


class StatusOut(BaseModel):
    last_scan_at: str | None
    last_scan_error: str | None
    last_scan_stats: dict[str, Any]
    next_scan_at: str | None
    refresh_minutes: int
    drop_threshold: float
    football_drop_threshold: float
    tracking_enabled: bool
    basketball_enabled: bool
    football_enabled: bool
    provider: str
    football_provider: str
    use_mock_data: bool
    requests_remaining: int | None
    quota_warning: str | None


class TrackingIn(BaseModel):
    enabled: bool


# ---- Endpoints -------------------------------------------------------------

LOW_QUOTA_THRESHOLD = 20  # warn when The Odds API credits fall to/below this

_PROVIDER_LABELS = {"theoddsapi": "The Odds API", "oddspapi": "OddsPapi"}


def _quota_warning() -> str | None:
    """Human message if any in-use provider is out of / low on quota."""
    in_use = {monitor.provider}
    if monitor.basketball_enabled:
        in_use.add("oddspapi")  # basketball always runs on OddsPapi
    if monitor.football_enabled:
        in_use.add(monitor.football_provider)
    msgs = []
    for key in sorted(in_use):
        client = monitor.clients.get(key)
        if client is None or client.use_mock:
            continue
        label = _PROVIDER_LABELS.get(key, key)
        if getattr(client, "quota_exhausted", False):
            msgs.append(f"{label}: quota esaurita")
        elif client.requests_remaining is not None and client.requests_remaining <= LOW_QUOTA_THRESHOLD:
            msgs.append(f"{label}: {client.requests_remaining} crediti rimasti")
    return " · ".join(msgs) if msgs else None


@app.get("/")
async def health():
    """Plain health check at the root so Render's health probe passes."""
    return {"service": "tennis-value-monitor", "status": "ok"}


@api.get("/")
async def root():
    return {"service": "tennis-value-monitor", "status": "ok"}


@api.get("/status", response_model=StatusOut)
async def get_status():
    job = scheduler.get_job("tennis-scan")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return StatusOut(
        last_scan_at=monitor.last_scan_at.isoformat() if monitor.last_scan_at else None,
        last_scan_error=monitor.last_scan_error,
        last_scan_stats=monitor.last_scan_stats,
        next_scan_at=next_run,
        refresh_minutes=REFRESH_MINUTES,
        drop_threshold=monitor.drop_threshold,
        football_drop_threshold=monitor.football_drop_threshold,
        tracking_enabled=monitor.tracking_enabled,
        basketball_enabled=monitor.basketball_enabled,
        football_enabled=monitor.football_enabled,
        provider=monitor.provider,
        football_provider=monitor.football_provider,
        use_mock_data=monitor.client.use_mock,
        requests_remaining=monitor.client.requests_remaining,
        quota_warning=_quota_warning(),
    )


@api.get("/snapshot")
async def get_snapshot():
    snap = await db.snapshots.find_one({"_id": "latest"}, {"_id": 0})
    if not snap:
        return {
            "updated_at": None,
            "provider": monitor.provider,
            "drop_threshold": monitor.drop_threshold,
            "tracking_enabled": monitor.tracking_enabled,
            "matches": [],
        }
    return snap


@api.get("/alerts")
async def list_alerts(limit: int = 50):
    docs = await db.alerts.find({}, {"_id": 0, "telegram_response": 0}).sort(
        "created_at", -1
    ).to_list(limit)
    return {"alerts": docs}


@api.post("/refresh")
async def manual_refresh():
    # Force a scan on demand even if tracking is currently paused.
    stats = await monitor.scan_once(force=True)
    return {"ok": True, "stats": stats}


@api.post("/tracking")
async def set_tracking(body: TrackingIn):
    enabled = await monitor.set_tracking(body.enabled)
    return {"tracking_enabled": enabled}


def _settings_out() -> SettingsOut:
    return SettingsOut(
        drop_threshold=monitor.drop_threshold,
        football_drop_threshold=monitor.football_drop_threshold,
        tracking_enabled=monitor.tracking_enabled,
        basketball_enabled=monitor.basketball_enabled,
        football_enabled=monitor.football_enabled,
        provider=monitor.provider,
        football_provider=monitor.football_provider,
        providers=list(monitor.clients.keys()),
        telegram_configured=bool(monitor.telegram.token and monitor.telegram.chat_id),
        refresh_minutes=REFRESH_MINUTES,
    )


@api.get("/settings", response_model=SettingsOut)
async def get_settings():
    return _settings_out()


@api.put("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsIn):
    if body.provider is not None and body.provider not in monitor.clients:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    if body.football_provider is not None and body.football_provider not in monitor.clients:
        raise HTTPException(400, f"Unknown football_provider: {body.football_provider}")
    await monitor.save_settings(
        drop_threshold=body.drop_threshold,
        football_drop_threshold=body.football_drop_threshold,
        tracking_enabled=body.tracking_enabled,
        basketball_enabled=body.basketball_enabled,
        football_enabled=body.football_enabled,
        provider=body.provider,
        football_provider=body.football_provider,
        telegram_token=body.telegram_token,
        telegram_chat_id=body.telegram_chat_id,
    )
    return _settings_out()


@api.post("/telegram/test")
async def telegram_test():
    result = await monitor.telegram.send_message(
        "✅ Tennis Value Monitor · test alert. Se leggi questo messaggio, il bot Telegram è collegato correttamente."
    )
    return {"ok": bool(result.get("ok")), "response": result}


@api.post("/mock/{enabled}")
async def toggle_mock(enabled: bool):
    """Toggle mock data mode for demo / when the API key is not active."""
    for c in monitor.clients.values():
        c.use_mock = enabled
    return {"use_mock_data": monitor.client.use_mock}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
