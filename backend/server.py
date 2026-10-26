"""Tennis Value-Bet Monitor - FastAPI server."""
from __future__ import annotations

import logging
import os
import re
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

from monitor import FAST_LOOP_SPORTS, ODDS_PROVIDERS, TennisMonitor

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("tennis-monitor")

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "10"))  # 6 refresh / ora
# Fast F1/MLB loop (prediction markets are keyless/free, so it doesn't consume
# any provider quota): alert latency is the priority, and a poll only fetches
# the games inside the 60-minute window, so poll every minute. 0 disables it
# (F1/MLB then run on the main loop). Tunable via F1_REFRESH_SECONDS.
F1_REFRESH_SECONDS = int(os.environ.get("F1_REFRESH_SECONDS", "60"))
# Whale monitor loop (keyless Polymarket Data API, one small request per poll
# thanks to the server-side size filter). 0 disables it.
WHALE_REFRESH_SECONDS = int(os.environ.get("WHALE_REFRESH_SECONDS", "30"))

mongo_url = os.environ["MONGO_URL"]
db_name = os.environ["DB_NAME"]
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]

monitor = TennisMonitor(db)
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await monitor.load_settings()
    await monitor.ensure_indexes()
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
        logger.info("Scan scheduled - every %d minutes", REFRESH_MINUTES)
    else:
        logger.info("Automatic scheduler disabled (REFRESH_MINUTES=%s) - scans are on-demand only", REFRESH_MINUTES)
    # Fast loop for sports whose data source is free (prediction markets):
    # F1/MLB scan every F1_REFRESH_SECONDS, independent of the paid
    # providers' cadence. Partial scans merge into the snapshot without
    # touching the main scan's status.
    if F1_REFRESH_SECONDS > 0:
        # The main scan leaves these sports to the fast loop.
        monitor.fast_loop_sports = FAST_LOOP_SPORTS
        scheduler.add_job(
            monitor.scan_once,
            trigger=IntervalTrigger(seconds=F1_REFRESH_SECONDS),
            id="pm-scan",
            next_run_time=datetime.now(timezone.utc),
            kwargs={"sports": FAST_LOOP_SPORTS, "update_state": False},
            max_instances=1,
            coalesce=True,
        )
        logger.info("Prediction-market fast loop scheduled - every %d seconds", F1_REFRESH_SECONDS)
    # Whale monitor: keyless Polymarket trade feed, independent of the odds scans
    # and the tracking toggle (gated only by whale_enabled inside scan_whales).
    if WHALE_REFRESH_SECONDS > 0:
        scheduler.add_job(
            monitor.scan_whales,
            trigger=IntervalTrigger(seconds=WHALE_REFRESH_SECONDS),
            id="whale-scan",
            next_run_time=datetime.now(timezone.utc),
            max_instances=1,
            coalesce=True,
        )
        logger.info("Whale monitor scheduled - every %d seconds", WHALE_REFRESH_SECONDS)
    # Start once, after both jobs are registered: the fast F1/MLB loop must
    # still run when REFRESH_MINUTES=0 (on-demand main scan), which the old
    # start-inside-the-REFRESH_MINUTES-branch placement silently skipped.
    if scheduler.get_jobs():
        scheduler.start()
        logger.info("Scheduler started")
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
    props_drop_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    tracking_enabled: bool | None = None
    tennis_enabled: bool | None = None
    basketball_enabled: bool | None = None
    football_enabled: bool | None = None
    hockey_enabled: bool | None = None
    volley_enabled: bool | None = None
    f1_enabled: bool | None = None
    mlb_enabled: bool | None = None
    nba_props_enabled: bool | None = None
    props_nba_enabled: bool | None = None
    props_seriea_enabled: bool | None = None
    props_epl_enabled: bool | None = None
    outright_enabled: bool | None = None
    whale_enabled: bool | None = None
    whale_min_usd: float | None = Field(default=None, ge=0.0)
    provider: str | None = None
    football_provider: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    oddspapi_api_key: str | None = None
    theoddsapi_api_key: str | None = None
    propline_api_key: str | None = None


class SettingsOut(BaseModel):
    drop_threshold: float
    football_drop_threshold: float
    props_drop_threshold: float
    tracking_enabled: bool
    tennis_enabled: bool
    basketball_enabled: bool
    football_enabled: bool
    hockey_enabled: bool
    volley_enabled: bool
    f1_enabled: bool
    mlb_enabled: bool
    nba_props_enabled: bool
    props_nba_enabled: bool
    props_seriea_enabled: bool
    props_epl_enabled: bool
    outright_enabled: bool
    whale_enabled: bool
    whale_min_usd: float
    provider: str
    football_provider: str
    providers: list[str]
    telegram_configured: bool
    refresh_minutes: int
    # Masked hint of the active OddsPapi key (last 4 chars) so the dashboard can
    # confirm which key is live without ever exposing the full value.
    oddspapi_key_hint: str | None
    theoddsapi_key_hint: str | None
    propline_key_hint: str | None
    # Result of the last PropLine request ("✓ key valida · N richieste rimaste
    # oggi" / "✗ key rifiutata ..."), so a bad key is visible in Settings.
    propline_status: str | None = None


class StatusOut(BaseModel):
    last_scan_at: str | None
    last_scan_error: str | None
    last_scan_stats: dict[str, Any]
    next_scan_at: str | None
    refresh_minutes: int
    drop_threshold: float
    football_drop_threshold: float
    props_drop_threshold: float
    tracking_enabled: bool
    tennis_enabled: bool
    basketball_enabled: bool
    football_enabled: bool
    hockey_enabled: bool
    volley_enabled: bool
    f1_enabled: bool
    mlb_enabled: bool
    nba_props_enabled: bool
    props_nba_enabled: bool
    props_seriea_enabled: bool
    props_epl_enabled: bool
    outright_enabled: bool
    whale_enabled: bool
    provider: str
    football_provider: str
    use_mock_data: bool
    requests_remaining: int | None
    quota_warning: str | None
    last_key_rotation_at: str | None
    last_key_rotation_error: str | None
    # PropLine daily quota as of its last response (dashboard counter).
    propline_requests_remaining: int | None = None
    propline_daily_limit: int | None = None
    propline_status: str | None = None
    propline_checked_at: str | None = None


class TrackingIn(BaseModel):
    enabled: bool


# ---- Endpoints -------------------------------------------------------------

LOW_QUOTA_THRESHOLD = 20  # warn when The Odds API credits fall to/below this

_PROVIDER_LABELS = {"theoddsapi": "The Odds API", "oddspapi": "OddsPapi"}


def _quota_warning() -> str | None:
    """Human message if any in-use provider is out of / low on quota."""
    # `provider` is the tennis odds source: only in use while tennis is on.
    in_use = {monitor.provider} if monitor.tennis_enabled else set()
    if monitor.basketball_enabled:
        in_use.add("oddspapi")  # basketball always runs on OddsPapi
    if monitor.football_enabled:
        in_use.add(monitor.football_provider)
    if monitor.hockey_enabled:
        in_use.add("oddspapi")  # hockey always runs on OddsPapi
    if monitor.volley_enabled:
        in_use.add("oddspapi")  # volleyball always runs on OddsPapi
    if ((monitor.props_nba_enabled or monitor.props_seriea_enabled
         or monitor.props_epl_enabled)
            and monitor.clients["propline"].api_key):
        in_use.add("propline")  # NBA book props
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
        props_drop_threshold=monitor.props_drop_threshold,
        tracking_enabled=monitor.tracking_enabled,
        tennis_enabled=monitor.tennis_enabled,
        basketball_enabled=monitor.basketball_enabled,
        football_enabled=monitor.football_enabled,
        hockey_enabled=monitor.hockey_enabled,
        volley_enabled=monitor.volley_enabled,
        f1_enabled=monitor.f1_enabled,
        mlb_enabled=monitor.mlb_enabled,
        nba_props_enabled=monitor.nba_props_enabled,
        props_nba_enabled=monitor.props_nba_enabled,
        props_seriea_enabled=monitor.props_seriea_enabled,
        props_epl_enabled=monitor.props_epl_enabled,
        outright_enabled=monitor.outright_enabled,
        whale_enabled=monitor.whale_enabled,
        provider=monitor.provider,
        football_provider=monitor.football_provider,
        use_mock_data=monitor.client.use_mock,
        requests_remaining=monitor.client.requests_remaining,
        quota_warning=_quota_warning(),
        last_key_rotation_at=monitor.last_rotation_at.isoformat() if monitor.last_rotation_at else None,
        last_key_rotation_error=monitor.last_rotation_error,
        **_propline_quota(),
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
    # Matches are stored per sport (each loop writes only its own sports);
    # flatten them for the dashboard, hiding sports that are toggled off.
    by_sport = snap.pop("by_sport", None)
    if by_sport:
        snap["matches"] = [m for sport, ms in by_sport.items()
                           if monitor.sport_enabled(sport) for m in (ms or [])]
    return snap


@api.get("/alerts")
async def list_alerts(limit: int = 50):
    # The Telegram HTML text and raw response aren't rendered by the dashboard:
    # leave them out of the payload it polls every few seconds.
    limit = max(1, min(limit, 500))
    docs = await db.alerts.find(
        {}, {"_id": 0, "telegram_response": 0, "message": 0}
    ).sort("created_at", -1).to_list(limit)
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
    op_key = monitor.clients["oddspapi"].api_key
    toa_key = monitor.clients["theoddsapi"].api_key
    pl_key = monitor.clients["propline"].api_key
    return SettingsOut(
        oddspapi_key_hint=f"…{op_key[-4:]}" if op_key else None,
        theoddsapi_key_hint=f"…{toa_key[-4:]}" if toa_key else None,
        propline_key_hint=f"…{pl_key[-4:]}" if pl_key else None,
        propline_status=_propline_status(),
        drop_threshold=monitor.drop_threshold,
        football_drop_threshold=monitor.football_drop_threshold,
        props_drop_threshold=monitor.props_drop_threshold,
        tracking_enabled=monitor.tracking_enabled,
        tennis_enabled=monitor.tennis_enabled,
        basketball_enabled=monitor.basketball_enabled,
        football_enabled=monitor.football_enabled,
        hockey_enabled=monitor.hockey_enabled,
        volley_enabled=monitor.volley_enabled,
        f1_enabled=monitor.f1_enabled,
        mlb_enabled=monitor.mlb_enabled,
        nba_props_enabled=monitor.nba_props_enabled,
        props_nba_enabled=monitor.props_nba_enabled,
        props_seriea_enabled=monitor.props_seriea_enabled,
        props_epl_enabled=monitor.props_epl_enabled,
        outright_enabled=monitor.outright_enabled,
        whale_enabled=monitor.whale_enabled,
        whale_min_usd=monitor.whale_min_usd,
        provider=monitor.provider,
        football_provider=monitor.football_provider,
        # Only the general odds providers are selectable for tennis/football
        # ('prediction' and 'propline' are sport-specific).
        providers=list(ODDS_PROVIDERS),
        telegram_configured=bool(monitor.telegram.token and monitor.telegram.chat_id),
        refresh_minutes=REFRESH_MINUTES,
    )


@api.get("/settings", response_model=SettingsOut)
async def get_settings():
    return _settings_out()


def _propline_quota() -> dict:
    pl = monitor.clients.get("propline")
    if pl is None or not getattr(pl, "api_key", None):
        return {}
    ok_at = getattr(pl, "last_ok_at", None)
    return {
        "propline_requests_remaining": getattr(pl, "requests_remaining", None),
        "propline_daily_limit": getattr(pl, "daily_limit", None),
        "propline_status": _propline_status(),
        "propline_checked_at": (datetime.fromtimestamp(ok_at, tz=timezone.utc).isoformat()
                                if ok_at else None),
    }


def _propline_status() -> str | None:
    fn = getattr(monitor.clients.get("propline"), "status_text", None)
    return fn() if callable(fn) else None


def _clean_key(raw: str | None) -> str | None:
    """Normalize a pasted API key. Accepts the bare key or the whole sample
    command PropLine e-mails ('curl "https://...&apiKey=KEY"'): the key is
    extracted from it. Blank -> None (unchanged)."""
    if raw is None:
        return None
    raw = raw.strip().strip('"').strip("'").strip()
    m = re.search(r"apiKey=([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    return raw or None


@api.put("/settings", response_model=SettingsOut)
async def update_settings(body: SettingsIn):
    if body.provider is not None and body.provider not in ODDS_PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    if body.football_provider is not None and body.football_provider not in ODDS_PROVIDERS:
        raise HTTPException(400, f"Unknown football_provider: {body.football_provider}")
    await monitor.save_settings(
        drop_threshold=body.drop_threshold,
        football_drop_threshold=body.football_drop_threshold,
        props_drop_threshold=body.props_drop_threshold,
        tracking_enabled=body.tracking_enabled,
        tennis_enabled=body.tennis_enabled,
        basketball_enabled=body.basketball_enabled,
        football_enabled=body.football_enabled,
        hockey_enabled=body.hockey_enabled,
        volley_enabled=body.volley_enabled,
        f1_enabled=body.f1_enabled,
        mlb_enabled=body.mlb_enabled,
        nba_props_enabled=body.nba_props_enabled,
        props_nba_enabled=body.props_nba_enabled,
        props_seriea_enabled=body.props_seriea_enabled,
        props_epl_enabled=body.props_epl_enabled,
        outright_enabled=body.outright_enabled,
        whale_enabled=body.whale_enabled,
        whale_min_usd=body.whale_min_usd,
        provider=body.provider,
        football_provider=body.football_provider,
        telegram_token=body.telegram_token,
        telegram_chat_id=body.telegram_chat_id,
        oddspapi_api_key=(body.oddspapi_api_key.strip() or None)
        if body.oddspapi_api_key is not None else None,
        theoddsapi_api_key=(body.theoddsapi_api_key.strip() or None)
        if body.theoddsapi_api_key is not None else None,
        propline_api_key=_clean_key(body.propline_api_key),
    )
    pl = monitor.clients.get("propline")
    if body.propline_api_key and getattr(pl, "api_key", None) and hasattr(pl, "check_key"):
        await pl.check_key()
    return _settings_out()


@api.post("/propline/check")
async def propline_check():
    """Test the current PropLine key with one request (dashboard button)."""
    return await monitor.clients["propline"].check_key()


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

# Strip whitespace so "https://a, https://b" (a common footgun) still works.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    # Wildcard origins can't be combined with credentials (browsers reject
    # `Access-Control-Allow-Origin: *` on credentialed requests, and Starlette
    # then drops the header entirely). The API is token-free and the frontend
    # sends no cookies, so only enable credentials when origins are explicit.
    allow_credentials="*" not in _cors_origins,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
