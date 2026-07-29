from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.operator_attention import restore_attention_queue
from app.pipeline.runner import get_pipeline_runner
from app.scrape_queue.activity import scrape_queue_is_active
from app.scrape_queue.poller import scrape_queue_poll
from app.notifications.telegram import telegram_review_poll

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _parse_run_time(run_time: str) -> tuple[int, int]:
    parts = run_time.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return hour, minute


def _scheduled_pipeline_run() -> None:
    settings = get_settings()
    if not settings.pipeline_enabled:
        logger.info("Scheduled pipeline skipped — PIPELINE_ENABLED is false")
        return
    logger.info("Starting scheduled pipeline run")
    runner = get_pipeline_runner()
    result = runner.run_all(trigger="scheduler")
    logger.info("Scheduled pipeline finished: %s", result)


def get_scrape_poll_interval_secs() -> int:
    """Faster interval when a lead is active (always-on MMM loop)."""
    from app.pipeline.runner import get_pipeline_runner

    runner = get_pipeline_runner()
    if runner.state.is_running:
        settings = get_settings()
        return max(int(settings.page_scrape_poll_secs), 60)
    settings = get_settings()
    if scrape_queue_is_active():
        return max(int(settings.scrape_active_poll_secs), 5)
    return max(int(settings.page_scrape_poll_secs), 5)


def reschedule_scrape_queue_poll() -> None:
    """Adjust poller cadence after each tick based on queue activity."""
    if _scheduler is None:
        return
    job = _scheduler.get_job("scrape_queue_poll")
    if job is None:
        return
    interval = get_scrape_poll_interval_secs()
    job.reschedule(trigger=IntervalTrigger(seconds=interval))
    logger.debug("Scrape queue poll rescheduled — every %ss", interval)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    hour, minute = _parse_run_time(settings.pipeline_run_time)

    restored = restore_attention_queue()
    if restored:
        logger.info("Restored %s operator attention item(s)", restored)

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _scheduled_pipeline_run,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="daily_pipeline",
        replace_existing=True,
        name="Daily Lead Pipeline",
    )
    if settings.scrape_queue_poll_enabled:
        idle_secs = max(int(settings.page_scrape_poll_secs), 5)
        active_secs = max(int(settings.scrape_active_poll_secs), 2)
        interval_secs = get_scrape_poll_interval_secs()
        _scheduler.add_job(
            scrape_queue_poll,
            trigger=IntervalTrigger(seconds=interval_secs),
            id="scrape_queue_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=interval_secs * 2,
            name="Scrape Queue Poller",
        )
        logger.info(
            "Scrape queue poller started — active every %ss, idle every %ss",
            active_secs,
            idle_secs,
        )
        scrape_queue_poll()
    if settings.telegram_configured:
        poll_secs = max(settings.telegram_poll_ms // 1000, 15)
        _scheduler.add_job(
            telegram_review_poll,
            trigger=IntervalTrigger(seconds=poll_secs),
            id="telegram_attention_poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=poll_secs * 2,
            name="Telegram Attention Poller",
        )
        logger.info("Telegram attention poller started — every %ss", poll_secs)
        telegram_review_poll()
    _scheduler.start()
    logger.info(
        "Scheduler started — daily pipeline at %02d:%02d (local time), enabled=%s",
        hour,
        minute,
        settings.pipeline_enabled,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def get_scheduler_status() -> dict:
    settings = get_settings()
    hour, minute = _parse_run_time(settings.pipeline_run_time)

    status: dict = {
        "enabled": settings.pipeline_enabled,
        "run_time": settings.pipeline_run_time,
        "run_time_display": f"{hour:02d}:{minute:02d}",
        "timezone": "local",
        "is_running": _scheduler is not None and _scheduler.running,
        "next_run": None,
    }

    if _scheduler is not None:
        job = _scheduler.get_job("daily_pipeline")
        if job and job.next_run_time:
            status["next_run"] = job.next_run_time.isoformat()

    return status
