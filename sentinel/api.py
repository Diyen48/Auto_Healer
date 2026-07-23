"""
FastAPI application — the ingestion layer of the Sentinel pipeline.

Routes:
    POST /webhook/crash  — receives crash alerts, pushes to Redis Stream
    GET  /health         — liveness probe
    GET  /status         — remediation history (last N events)
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException

from sentinel.config import get_settings
from sentinel.models import CrashAlert, CrashEvent

logger = logging.getLogger("sentinel.api")

# ── Redis connection (module-level, initialised on startup) ─────────
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return the shared async Redis connection."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(
            settings.redis_url, decode_responses=True
        )
    return _redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks for the FastAPI application."""
    global _redis
    settings = get_settings()

    # ── Startup ─────────────────────────────────────────────────────
    logger.info("🚀 Sentinel API starting up …")
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    # Ensure the consumer group exists (MKSTREAM creates the stream too)
    try:
        await _redis.xgroup_create(
            settings.redis_stream,
            settings.redis_consumer_group,
            id="0",
            mkstream=True,
        )
        logger.info(
            "✅ Created consumer group '%s' on stream '%s'",
            settings.redis_consumer_group,
            settings.redis_stream,
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.info("ℹ️  Consumer group already exists — reusing.")
        else:
            raise

    yield

    # ── Shutdown ────────────────────────────────────────────────────
    if _redis:
        await _redis.aclose()
        _redis = None
    logger.info("🛑 Sentinel API shut down.")


# ── FastAPI App ─────────────────────────────────────────────────────
app = FastAPI(
    title="Sentinel — Auto-Remediation Pipeline",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/webhook/crash", status_code=202)
@app.post("/api/v1/crashes", status_code=202)
async def receive_crash(alert: CrashAlert):
    """
    Ingest a crash alert from a failing service.

    The alert is enriched into a CrashEvent, serialised to JSON,
    and appended to the Redis Stream for asynchronous processing
    by the worker.
    """
    r = await get_redis()
    settings = get_settings()

    event = CrashEvent.from_alert(alert)
    payload = {"data": event.model_dump_json()}

    try:
        msg_id = await r.xadd(settings.redis_stream, payload)
    except Exception as exc:
        logger.error("❌ Failed to enqueue crash event: %s", exc)
        raise HTTPException(status_code=503, detail="Queue unavailable") from exc

    logger.info(
        "📥 Crash event %s queued (stream msg %s)", event.event_id, msg_id
    )
    return {
        "status": "accepted",
        "event_id": event.event_id,
        "stream_msg_id": msg_id,
    }


@app.get("/health")
async def health():
    """Liveness probe — checks Redis connectivity."""
    try:
        r = await get_redis()
        await r.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Redis unreachable")


@app.get("/status")
async def status():
    """Return the last 20 crash events from the stream."""
    r = await get_redis()
    settings = get_settings()

    try:
        # XREVRANGE returns newest-first
        messages = await r.xrevrange(settings.redis_stream, count=20)
        events = []
        for msg_id, fields in messages:
            try:
                event_data = json.loads(fields.get("data", "{}"))
                events.append({"stream_id": msg_id, **event_data})
            except json.JSONDecodeError:
                events.append({"stream_id": msg_id, "raw": fields})
        return {"count": len(events), "events": events}
    except Exception as exc:
        logger.error("❌ Failed to read stream: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
