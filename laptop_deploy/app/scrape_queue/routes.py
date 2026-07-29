"""API routes for Step 3 Scrape Queue."""

from __future__ import annotations

from fastapi import APIRouter

from app.scrape_queue import get_scrape_queue

router = APIRouter(prefix="/api/step3/queue", tags=["step3"])


@router.get("/status")
async def queue_status():
    return get_scrape_queue().get_status()


@router.post("/enqueue")
async def enqueue_next():
    result = get_scrape_queue().enqueue_next_lead()
    return {
        "ok": result.ok,
        "message": result.message,
        "source_row": result.source_row,
        "link": result.link,
    }


@router.post("/finalize")
async def finalize_ready():
    result = get_scrape_queue().finalize_if_ready()
    return {
        "ok": result.ok,
        "message": result.message,
        "action": result.action,
        "source_row": result.source_row,
        "stats": result.stats,
    }


@router.post("/tick")
async def queue_tick():
    """Run one worker cycle (finalize + enqueue if idle)."""
    return get_scrape_queue().tick()
