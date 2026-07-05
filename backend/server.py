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

from monitor import TennisMonitor, SOFT_BOOKS_DEFAULT

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
    edge_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    soft_books: list[str] | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class SettingsOut(BaseModel):
    edge_threshold: float
    soft_books: list[str]
    telegram_configured: bool
    refresh_minutes: int


class StatusOut(BaseModel):
    last_scan_at: str | None
    last_scan_error: str | None
    last_scan_stats: dict[str, Any]
    next_scan_at: str | None
    refresh_minutes: int
    edge_threshold: float
    use_mock_data: bool


# ---- Endpoints -------------------------------------------------------------

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
        edge_threshold=monitor.edge_threshold,
        use_mock_data=monitor.oddspapi.use_mock,
    )


@api.get("/snapshot")
async def get_snapshot():
    snap = await db.snapshots.find_one({"_id": "latest"}, {"_id": 0})
    if not snap:
        return {
            "updated_at": None,
            "edge_threshold": monitor.edge_threshold,
            "soft_books": monitor.soft_books,
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
    stats = await monitor.scan_once()
    return {"ok": True, "stats": stats}


@api.get("/settings", response_model=SettingsOut)
async def get_settings():
    return SettingsOut(
        edge_threshold=monitor.edge_threshold,
        soft_books=monitor.soft_books,
        telegram_configured=bool(monitor.telegram.token and monitor.telegram.chat_id),
        refresh_minutes=REFRESH_MINUTES,
    )


@api.put("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsIn):
    if body.soft_books is not None:
        allowed = {"bet365", "betfair", "snai", "eurobet", "goldbet"}
        invalid = [b for b in body.soft_books if b not in allowed]
        if invalid:
            raise HTTPException(400, f"Invalid soft books: {invalid}")
        if not body.soft_books:
            raise HTTPException(400, "Select at least one soft book")
    await monitor.save_settings(
        edge_threshold=body.edge_threshold,
        soft_books=body.soft_books,
        telegram_token=body.telegram_token,
        telegram_chat_id=body.telegram_chat_id,
    )
    return SettingsOut(
        edge_threshold=monitor.edge_threshold,
        soft_books=monitor.soft_books,
        telegram_configured=bool(monitor.telegram.token and monitor.telegram.chat_id),
        refresh_minutes=REFRESH_MINUTES,
    )


@api.post("/telegram/test")
async def telegram_test():
    result = await monitor.telegram.send_message(
        "✅ Tennis Value Monitor · test alert. Se leggi questo messaggio, il bot Telegram è collegato correttamente."
    )
    return {"ok": bool(result.get("ok")), "response": result}


@api.post("/mock/{enabled}")
async def toggle_mock(enabled: bool):
    """Toggle mock data mode for demo / when OddsPapi key is not active."""
    monitor.oddspapi.use_mock = enabled
    return {"use_mock_data": monitor.oddspapi.use_mock}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
