"""Minimal API for the Windows scrape laptop."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.scrape_queue.routes import router as step3_queue_router
from app.scrape_queue import get_scrape_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Scrape laptop started on %s:%s", settings.host, settings.port)
    yield
    logger.info("Scrape laptop stopped")


app = FastAPI(title="Lead Gen Scrape Laptop", version="1.0.0", lifespan=lifespan)
app.include_router(step3_queue_router)


@app.get("/api/health")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "role": "scrape_laptop",
        "sheets_configured": settings.sheets_configured,
        "queue": get_scrape_queue().get_status(),
    }
