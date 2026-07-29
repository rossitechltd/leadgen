from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.scrape_queue import get_scrape_queue
from app.leads.parse_upload import LeadImportError
from app.pipeline.steps.step1_import_leads import import_leads_from_text
from app.scrape_queue.routes import router as step3_queue_router
from app.config import get_settings
from app.notifications.telegram import is_telegram_configured, get_telegram_poll_secs
from app.operator_attention import (
    get_attention_queue,
    remove_attention_by_kind,
    remove_attention_item,
    restore_attention_queue,
)
from app.pipeline.runner import STEP_DEFINITIONS, get_pipeline_runner
from app.pipeline.step1_checkpoint import has_checkpoint, load_checkpoint
from app.scheduler import get_scheduler_status, start_scheduler, stop_scheduler
from app.scrapers.apify_members import (
    ApifyConfigError,
    cookies_file_status,
    parse_cookies_json,
    write_cookies_file,
)
from app.scrapers.facebook_groups import load_groups
from app.sheets.client import SheetsError, get_sheets_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
    force=True,
)
# Pipeline + scraper logs always on the terminal
for _name in (
    "app.pipeline",
    "app.pipeline.steps",
    "app.scrapers",
    "app.scrapers.apify_members",
    "app.scrape_queue",
):
    logging.getLogger(_name).setLevel(logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    restore_attention_queue()
    cleared = remove_attention_by_kind("scrape_paste_mistake")
    if cleared:
        logger.info(
            "Removed %s legacy scrape paste mistake attention item(s) (alerts disabled)",
            cleared,
        )
    start_scheduler()
    logger.info("Lead Gen Pipeline app started")
    yield
    stop_scheduler()
    logger.info("Lead Gen Pipeline app stopped")


app = FastAPI(title="Lead Gen Pipeline", version="0.1.0", lifespan=lifespan)
app.include_router(step3_queue_router)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "steps": STEP_DEFINITIONS,
            "settings": {
                "pipeline_run_time": settings.pipeline_run_time,
                "pipeline_enabled": settings.pipeline_enabled,
                "sheet_dynamic_lead": settings.sheet_dynamic_lead,
                "sheet_all_imported": settings.sheet_all_imported,
                "sheet_ready_to_contact": settings.sheet_ready_to_contact,
                "sheet_scrape_queue": settings.sheet_scrape_queue,
                "sheets_configured": settings.sheets_configured,
            },
        },
    )


@app.get("/api/health")
async def health():
    settings = get_settings()
    sheets_status: dict = {
        "configured": settings.sheets_configured,
        "connected": False,
        "error": None,
        "spreadsheet": None,
    }

    if settings.sheets_configured:
        try:
            client = get_sheets_client()
            meta = client.ping()
            sheets_status["connected"] = True
            sheets_status["spreadsheet"] = meta
        except SheetsError as exc:
            sheets_status["error"] = str(exc)
        except Exception as exc:
            sheets_status["error"] = str(exc)
    else:
        sheets_status["error"] = (
            f"Service account key not found — place autoleadverification JSON in project root"
        )

    return {
        "status": "ok",
        "sheets": sheets_status,
        "scheduler": get_scheduler_status(),
    }


@app.get("/api/status")
async def status():
    runner = get_pipeline_runner()
    settings = get_settings()
    checkpoint = load_checkpoint()
    return {
        "pipeline": runner.get_status(),
        "scheduler": get_scheduler_status(),
        "telegram": {
            "configured": is_telegram_configured(),
            "poll_sec": get_telegram_poll_secs(),
        },
        "attention": {
            "count": len(get_attention_queue()),
            "items": get_attention_queue(),
        },
        "step1_checkpoint": {
            "active": checkpoint is not None,
            "next_index": checkpoint.get("next_index") if checkpoint else None,
            "group_count": len(checkpoint.get("group_urls") or []) if checkpoint else 0,
            "paused_reason": checkpoint.get("paused_reason") if checkpoint else None,
        },
        "config": {
            "pipeline_run_time": settings.pipeline_run_time,
            "pipeline_enabled": settings.pipeline_enabled,
            "sheet_dynamic_lead": settings.sheet_dynamic_lead,
            "sheet_all_imported": settings.sheet_all_imported,
            "sheet_ready_to_contact": settings.sheet_ready_to_contact,
            "sheet_scrape_queue": settings.sheet_scrape_queue,
            "sheets_configured": settings.sheets_configured,
            "dashboard_url": settings.dashboard_url,
        },
        "timing": {
            "scrape_stall_secs": settings.scrape_stall_secs,
            "per_lead_scrape_secs": max(float(settings.scrape_stall_secs) * 1.5, 90.0),
            "entity_classify_batch_size": settings.entity_classify_batch_size,
            "refine_batch_size": settings.refine_batch_size,
            "qualify_website_timeout_secs": settings.qualify_website_timeout_secs,
        },
    }


@app.get("/api/attention")
async def attention_queue():
    return {"items": get_attention_queue(), "count": len(get_attention_queue())}


@app.post("/api/attention/{item_id}/dismiss")
async def dismiss_attention(item_id: str):
    if not remove_attention_item(item_id):
        raise HTTPException(status_code=404, detail="Attention item not found")
    return {"ok": True, "items": get_attention_queue()}


@app.get("/api/leads/status")
async def leads_status():
    return get_scrape_queue().get_list_progress()


@app.get("/api/scrape-progress")
async def scrape_progress():
    return get_scrape_queue().get_list_progress()


@app.post("/api/scrape/mark-complete")
async def mark_scrape_complete():
    """Mark page scrape complete without moving data between spreadsheets."""
    from app.pipeline.runner import get_pipeline_runner
    from app.pipeline.steps.base import StepStatus

    queue = get_scrape_queue()
    result = await asyncio.to_thread(queue.request_manual_scrape_complete)

    runner = get_pipeline_runner()
    step4 = next((s for s in runner.state.steps if s.id == 4), None)
    if step4 and step4.status in {
        StepStatus.RUNNING,
        StepStatus.WAITING,
        StepStatus.IDLE,
    }:
        if runner.state.is_running and runner.state.current_step_id == 4:
            runner._append_logs(
                ["Manual scrape complete requested — Step 4 will finish shortly"]
            )
        else:
            await asyncio.to_thread(
                runner.mark_step_manual_complete,
                4,
                result["message"],
                {"manual_complete": True},
            )

    return result


@app.post("/api/leads/upload")
async def upload_leads(body: dict = Body(default_factory=dict)):
    settings = get_settings()
    if not settings.sheets_configured:
        raise HTTPException(
            status_code=400,
            detail=f"Google Sheets not configured — place service account JSON in project root",
        )

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            status_code=400,
            detail="Paste or upload lead list in 'content' (Facebook Link + Business Name).",
        )

    try:
        stats = await asyncio.to_thread(import_leads_from_text, content)
    except LeadImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Lead upload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"ok": True, **stats}


@app.get("/api/step1/checkpoint")
async def step1_checkpoint_status():
    checkpoint = load_checkpoint()
    if not checkpoint:
        return {"active": False}
    return {
        "active": True,
        "next_index": checkpoint.get("next_index"),
        "group_count": len(checkpoint.get("group_urls") or []),
        "paused_reason": checkpoint.get("paused_reason"),
        "stats": checkpoint.get("stats") or {},
    }


@app.post("/api/pipeline/steps/1/resume")
async def resume_step1():
    runner = get_pipeline_runner()
    if not has_checkpoint():
        raise HTTPException(status_code=404, detail="No Step 1 checkpoint to resume")
    options = {"step1": {"resume_checkpoint": True}}
    result = await asyncio.to_thread(
        runner.run_step, 1, trigger="manual", options=options
    )
    if not result.get("ok"):
        status_code = 409 if "already running" in result.get("error", "").lower() else 400
        raise HTTPException(status_code=status_code, detail=result.get("error", "Error"))
    return result


@app.get("/api/fb-cookies")
async def fb_cookies_status():
    settings = get_settings()
    return cookies_file_status(settings.fb_cookies_path)


@app.post("/api/fb-cookies")
async def save_fb_cookies(body: dict = Body(default_factory=dict)):
    settings = get_settings()
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="Paste Cookie-Editor JSON in 'content'.")

    try:
        cookies = parse_cookies_json(content)
        write_cookies_file(settings.fb_cookies_path, cookies)
    except ApifyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status = cookies_file_status(settings.fb_cookies_path)
    logger.info(
        "Updated Facebook cookies at %s (%s cookies)",
        settings.fb_cookies_path,
        status.get("cookie_count", 0),
    )

    auto_resume: dict[str, Any] = {"attempted": False}
    if status.get("configured") and has_checkpoint():
        checkpoint = load_checkpoint()
        if checkpoint and checkpoint.get("paused_reason") == "cookies_invalid":
            runner = get_pipeline_runner()
            if not runner.state.is_running:
                auto_resume["attempted"] = True
                auto_resume["result"] = await asyncio.to_thread(
                    runner.run_step,
                    1,
                    trigger="cookies_updated",
                    options={"step1": {"resume_checkpoint": True}},
                )

    return {
        "ok": True,
        "message": f"Saved {status.get('cookie_count', 0)} cookies",
        "auto_resume": auto_resume,
        **status,
    }


@app.get("/api/groups")
async def list_groups():
    settings = get_settings()
    try:
        groups = load_groups(settings.fb_groups_path)
    except ApifyConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "groups": groups,
        "count": len(groups),
        "default_members_per_group": settings.apify_member_count,
    }


@app.get("/api/pipeline/logs")
async def pipeline_logs(limit: int = 100):
    runner = get_pipeline_runner()
    return {"logs": runner.get_logs(limit=limit)}


@app.post("/api/pipeline/abandon")
async def abandon_pipeline():
    runner = get_pipeline_runner()
    result = runner.request_abandon()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "Cannot abandon"))
    return result


@app.post("/api/pipeline/run")
async def run_pipeline(body: dict = Body(default_factory=dict)):
    runner = get_pipeline_runner()
    options = body.get("options") or {}
    result = await asyncio.to_thread(
        runner.run_all, trigger="manual", options=options
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "Pipeline busy"))
    return result


@app.post("/api/pipeline/steps/{step_id}/run")
async def run_single_step(step_id: int, body: dict = Body(default_factory=dict)):
    runner = get_pipeline_runner()
    options = body.get("options") or {}
    result = await asyncio.to_thread(
        runner.run_step, step_id, trigger="manual", options=options
    )
    if not result.get("ok"):
        status_code = 404 if "Unknown step" in result.get("error", "") else 409
        raise HTTPException(status_code=status_code, detail=result.get("error", "Error"))
    return result
