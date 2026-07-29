"""FastAPI routes for Step 3 Mini Mouse Macro coordination."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.automation.coordinator import get_coordinator

router = APIRouter(prefix="/api/step3/coordinator", tags=["step3"])


class MoveToTopRequest(BaseModel):
    row_index: int | None = Field(
        default=None,
        description="1-based sheet row to move. Omit to auto-pick first pending row not at top.",
    )
    wait_safe: bool = Field(
        default=True,
        description="Wait for Loop 1 SAFE before moving (required for macro mode).",
    )


class MmmSignalRequest(BaseModel):
    signal: str
    detail: str = ""


@router.get("/status")
async def coordinator_status():
    return get_coordinator().get_status()


@router.post("/start-loop1")
async def start_loop1():
    return get_coordinator().start_loop1()


@router.post("/stop")
async def stop_coordinator():
    return get_coordinator().stop()


@router.post("/move-to-top")
async def move_to_top(body: MoveToTopRequest | None = None):
    body = body or MoveToTopRequest()
    result = get_coordinator().move_to_top(
        body.row_index,
        wait_safe=body.wait_safe,
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {
        "ok": True,
        "message": result.message,
        "row_index": result.row_index,
        "moved_via": result.moved_via,
        "stats": result.stats,
    }


@router.post("/process-queue")
async def process_queue(max_moves: int = 1):
    result = get_coordinator().process_pending_queue(max_moves=max_moves)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["moves"][-1]["message"])
    return result


@router.post("/mmm-signal")
async def mmm_signal(body: MmmSignalRequest):
    """
    Endpoint for MMM macros or helper scripts to report SAFE / BUSY / LOOP2_DONE.

    Example: curl -X POST http://127.0.0.1:8000/api/step3/coordinator/mmm-signal \\
      -H 'Content-Type: application/json' -d '{"signal":"SAFE"}'
    """
    return get_coordinator().report_mmm_signal(body.signal, body.detail)
