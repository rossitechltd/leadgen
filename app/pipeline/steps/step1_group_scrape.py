from __future__ import annotations

import logging
import time
from typing import Any

import sheets
from app.config import get_settings
from app.notifications.telegram import notify_attention
from app.operator_attention import add_attention_item, remove_attention_by_kind
from app.pipeline.step1_checkpoint import clear_checkpoint, load_checkpoint, save_checkpoint
from app.pipeline.steps.base import PipelineContext, StepResult, StepStatus
from app.scrapers.apify_members import (
    ApifyConfigError,
    PAGINATION_PAUSE_SECS,
    is_cookie_related_error,
    scrape_group_members,
)
from app.scrapers.facebook_groups import load_groups, normalize_group_url
from app.scrapers.lead_mapping import (
    imported_facebook_links,
    lead_row_for_sheet,
    member_to_lead,
    normalize_facebook_url,
    sort_members_by_joined_at,
)
from app.scrape_queue import get_scrape_queue
from app.sheets.columns import DYNAMIC_LEAD_HEADERS

logger = logging.getLogger(__name__)

COOKIE_ATTENTION_KIND = "cookie_refresh"
COOKIE_ATTENTION_ID = "step1-cookie-refresh"


def _existing_links(settings) -> tuple[set[str], set[str], set[str]]:
    dynamic: set[str] = set()
    imported: set[str] = set()
    try:
        for row in sheets.read_all(settings.sheet_dynamic_lead):
            raw = row.get("Facebook Link") or ""
            if raw:
                dynamic.add(normalize_facebook_url(str(raw)))
    except sheets.SheetsError as exc:
        logger.warning("Could not read %s for dedupe: %s", settings.sheet_dynamic_lead, exc)

    try:
        imported = imported_facebook_links(sheets.read_all(settings.sheet_all_imported))
    except sheets.SheetsError as exc:
        logger.warning("Could not read %s for dedupe: %s", settings.sheet_all_imported, exc)

    return dynamic, imported, dynamic | imported


def _resolve_group_urls(settings, step_opts: dict[str, Any]) -> list[str]:
    all_groups = load_groups(settings.fb_groups_path)
    known = {g["url"].lower(): g["url"] for g in all_groups}
    mode = str(step_opts.get("mode") or "all").strip().lower()

    if mode == "single":
        selected = normalize_group_url(str(step_opts.get("group_url") or ""))
        if not selected:
            raise ApifyConfigError("Select a group URL for single-group scrape.")
        if selected.lower() in known:
            return [known[selected.lower()]]
        return [selected]

    if mode == "two":
        raw_urls = step_opts.get("group_urls")
        if isinstance(raw_urls, list) and len(raw_urls) >= 2:
            candidates = [
                normalize_group_url(str(u or ""))
                for u in raw_urls[:2]
            ]
        else:
            candidates = [
                normalize_group_url(str(step_opts.get("group_url") or "")),
                normalize_group_url(str(step_opts.get("group_url_2") or "")),
            ]
        if not candidates[0] or not candidates[1]:
            raise ApifyConfigError("Select two group URLs for two-group scrape.")
        if candidates[0].lower() == candidates[1].lower():
            raise ApifyConfigError("Select two different groups.")
        resolved: list[str] = []
        for url in candidates:
            resolved.append(known.get(url.lower(), url))
        return resolved

    return [g["url"] for g in all_groups]


def _default_stats(
    mode: str,
    member_count: int,
    group_count: int,
) -> dict[str, Any]:
    members_target = member_count * group_count
    return {
        "groups_scraped": 0,
        "members_fetched": 0,
        "no_link_skipped": 0,
        "duplicates_skipped": 0,
        "duplicate_dynamic": 0,
        "duplicate_imported": 0,
        "duplicate_run": 0,
        "rows_appended": 0,
        "groups_failed": 0,
        "mode": mode,
        "members_per_group": member_count,
        "members_target": members_target,
        "group_count": group_count,
    }


def _process_members_for_append(
    members: list[dict[str, Any]],
    *,
    dynamic_links: set[str],
    imported_links: set[str],
    seen_this_run: set[str],
    stats: dict[str, Any],
) -> list[list[str]]:
    rows: list[list[str]] = []
    for member in sort_members_by_joined_at(members):
        lead = member_to_lead(member)
        if not lead:
            stats["no_link_skipped"] += 1
            continue

        link = lead["facebook_link"]
        if link in seen_this_run:
            stats["duplicate_run"] += 1
            stats["duplicates_skipped"] += 1
            continue
        if link in dynamic_links:
            stats["duplicate_dynamic"] += 1
            stats["duplicates_skipped"] += 1
            continue
        if link in imported_links:
            stats["duplicate_imported"] += 1
            stats["duplicates_skipped"] += 1
            continue

        seen_this_run.add(link)
        dynamic_links.add(link)
        rows.append(lead_row_for_sheet(lead))
    return rows


def _append_rows(settings, rows: list[list[str]], ctx: PipelineContext) -> int:
    if not rows:
        return 0
    sheets.ensure_worksheet(settings.sheet_dynamic_lead, DYNAMIC_LEAD_HEADERS)
    sheets.prepend_rows(settings.sheet_dynamic_lead, rows)
    get_scrape_queue().refresh_lead_index()
    ctx.add_log(f"Step 1: inserted {len(rows)} row(s) at top of Dynamic Lead Sheet")
    return len(rows)


def _pause_for_cookies(
    ctx: PipelineContext,
    *,
    settings,
    step_opts: dict[str, Any],
    group_urls: list[str],
    next_index: int,
    member_count: int,
    mode: str,
    stats: dict[str, Any],
    error_message: str,
) -> StepResult:
    save_checkpoint(
        {
            "step_opts": step_opts,
            "group_urls": group_urls,
            "next_index": next_index,
            "members_per_group": member_count,
            "mode": mode,
            "stats": stats,
            "paused_reason": "cookies_invalid",
            "paused_message": error_message,
        }
    )

    group_num = min(next_index + 1, len(group_urls))
    title = f"Step 1 paused at group {group_num}/{len(group_urls)}"
    body = (
        f"{error_message}\n"
        "Paste fresh Facebook cookies in the dashboard, then resume Step 1."
    )
    add_attention_item(
        kind=COOKIE_ATTENTION_KIND,
        title=title,
        body=body,
        detail=error_message,
        item_id=COOKIE_ATTENTION_ID,
    )
    notify_attention(title, body, context="Facebook cookies needed")

    ctx.add_log(f"Step 1: paused — {error_message}")
    return StepResult(
        status=StepStatus.WAITING,
        message=(
            f"Paused at group {group_num}/{len(group_urls)} — "
            "paste fresh cookies and resume Step 1"
        ),
        stats={
            **stats,
            "paused_at_group": group_num,
            "paused_reason": "cookies_invalid",
            "checkpoint_saved": True,
        },
    )


def run(ctx: PipelineContext) -> StepResult:
    """
    Step 1: Scrape Facebook groups via Apify and append leads to Dynamic Lead Sheet.

  Options (ctx.step_options["step1"]):
    - mode: "all" | "single" | "two"
    - group_url: URL when mode is single
    - group_urls: [url1, url2] when mode is two
    - members_per_group: max members to fetch per group
    - resume_checkpoint: true to continue after cookie pause
    """
    settings = get_settings()
    step_opts = dict(ctx.step_options.get("step1") or {})
    resume_checkpoint = bool(step_opts.get("resume_checkpoint"))

    if not settings.apify_configured:
        ctx.add_log("Step 1: missing APIFY_API_TOKEN in .env")
        return StepResult(
            status=StepStatus.FAILED,
            message="Set APIFY_API_TOKEN in .env",
            stats={},
        )

    if not settings.fb_cookies_configured:
        checkpoint = load_checkpoint()
        message = f"Export Facebook cookies to {settings.fb_cookies_path.name}"
        ctx.add_log(f"Step 1: missing cookies at {settings.fb_cookies_path}")
        if checkpoint or resume_checkpoint:
            return _pause_for_cookies(
                ctx,
                settings=settings,
                step_opts=step_opts,
                group_urls=checkpoint.get("group_urls", []) if checkpoint else [],
                next_index=checkpoint.get("next_index", 0) if checkpoint else 0,
                member_count=int(
                    (checkpoint or {}).get("members_per_group")
                    or settings.apify_member_count
                    or 20
                ),
                mode=str((checkpoint or {}).get("mode") or "all"),
                stats=(checkpoint or {}).get("stats") or {},
                error_message=message,
            )
        return StepResult(status=StepStatus.FAILED, message=message, stats={})

    if not settings.sheets_configured:
        ctx.add_log(
            f"Step 1: Google Sheets key not found at {settings.service_account_path}"
        )
        return StepResult(
            status=StepStatus.FAILED,
            message=f"Service account JSON not found: {settings.service_account_path.name}",
            stats={},
        )

    checkpoint = load_checkpoint() if resume_checkpoint else None
    if resume_checkpoint and not checkpoint:
        ctx.add_log("Step 1: no checkpoint found — starting fresh scrape")
        resume_checkpoint = False

    if resume_checkpoint and checkpoint:
        step_opts = dict(checkpoint.get("step_opts") or step_opts)
        group_urls = list(checkpoint.get("group_urls") or [])
        start_index = int(checkpoint.get("next_index") or 0)
        member_count = int(checkpoint.get("members_per_group") or settings.apify_member_count or 20)
        mode = str(checkpoint.get("mode") or step_opts.get("mode") or "all")
        stats = dict(checkpoint.get("stats") or _default_stats(mode, member_count, len(group_urls)))
        ctx.add_log(
            f"Step 1: resuming from group {start_index + 1}/{len(group_urls)} "
            f"(checkpoint)"
        )
    else:
        clear_checkpoint()
        remove_attention_by_kind(COOKIE_ATTENTION_KIND)
        try:
            group_urls = _resolve_group_urls(settings, step_opts)
        except ApifyConfigError as exc:
            ctx.add_log(f"Step 1: {exc}")
            return StepResult(status=StepStatus.FAILED, message=str(exc), stats={})

        raw_member_count = step_opts.get("members_per_group")
        if raw_member_count is None:
            member_count = settings.apify_member_count
        else:
            try:
                member_count = int(raw_member_count)
            except (TypeError, ValueError):
                return StepResult(
                    status=StepStatus.FAILED,
                    message="members_per_group must be a number",
                    stats={},
                )
            if member_count < 1:
                return StepResult(
                    status=StepStatus.FAILED,
                    message="members_per_group must be at least 1",
                    stats={},
                )

        mode = str(step_opts.get("mode") or "all").strip().lower()
        start_index = 0
        stats = _default_stats(mode, member_count, len(group_urls))

    if not group_urls:
        return StepResult(
            status=StepStatus.FAILED,
            message="No group URLs to scrape",
            stats=stats,
        )

    members_target = member_count * len(group_urls)
    stats["members_target"] = members_target
    stats["group_count"] = len(group_urls)

    ctx.add_log(
        f"Step 1: scraping {len(group_urls)} group(s) "
        f"(mode={mode}, members_per_group={member_count}, "
        f"max_members={members_target}, start_index={start_index + 1})"
    )

    dynamic_links, imported_links, _ = _existing_links(settings)
    ctx.add_log(
        f"Step 1: {len(dynamic_links)} on Dynamic Lead Sheet, "
        f"{len(imported_links)} in allimported (dedupe)"
    )

    seen_this_run: set[str] = set()
    scrape_kwargs = {
        "api_token": settings.apify_api_token,
        "cookies_path": settings.fb_cookies_path,
        "actor_id": settings.apify_actor_id,
        "min_delay": settings.apify_min_delay,
        "max_delay": settings.apify_max_delay,
        "proxy_country": settings.apify_proxy_country,
        "proxy_groups": settings.apify_proxy_groups,
        "count": member_count,
    }

    for idx in range(start_index, len(group_urls)):
        if ctx.is_abandoned():
            ctx.add_log("Step 1: abandon requested — stopping group scrape loop")
            save_checkpoint(
                {
                    "step_opts": step_opts,
                    "group_urls": group_urls,
                    "next_index": idx,
                    "members_per_group": member_count,
                    "mode": mode,
                    "stats": stats,
                    "paused_reason": "abandoned",
                }
            )
            return StepResult(
                status=StepStatus.ABANDONED,
                message=f"Abandoned at group {idx + 1}/{len(group_urls)}",
                stats=stats,
            )

        group_url = group_urls[idx]
        if idx > start_index or idx > 0:
            ctx.add_log(
                f"Step 1: pausing {PAGINATION_PAUSE_SECS}s before next group "
                "(protects Facebook session)"
            )
            time.sleep(PAGINATION_PAUSE_SECS)

        ctx.add_log(f"Step 1: group {idx + 1}/{len(group_urls)}: {group_url}")

        members: list[dict[str, Any]] | None = None
        last_error: str | None = None
        cookie_failure = False

        for attempt in range(2):
            try:
                members = scrape_group_members(group_url=group_url, **scrape_kwargs)
                break
            except Exception as exc:
                last_error = str(exc)
                if is_cookie_related_error(exc):
                    cookie_failure = True
                    break
                if attempt == 0:
                    ctx.add_log(
                        f"Step 1: group {idx + 1} failed, retrying after "
                        f"{PAGINATION_PAUSE_SECS}s: {exc}"
                    )
                    time.sleep(PAGINATION_PAUSE_SECS)
                else:
                    logger.error("Group scrape failed: %s — %s", group_url, exc)

        if cookie_failure:
            return _pause_for_cookies(
                ctx,
                settings=settings,
                step_opts=step_opts,
                group_urls=group_urls,
                next_index=idx,
                member_count=member_count,
                mode=mode,
                stats=stats,
                error_message=last_error or "Facebook cookies invalid or expired",
            )

        if members is None:
            stats["groups_failed"] += 1
            ctx.add_log(f"Step 1: failed group {idx + 1}/{len(group_urls)}: {last_error}")
            continue

        stats["groups_scraped"] += 1
        stats["members_fetched"] += len(members)
        ctx.add_log(
            f"Step 1: group {idx + 1}/{len(group_urls)} fetched "
            f"{len(members)} member(s)"
        )

        group_rows = _process_members_for_append(
            members,
            dynamic_links=dynamic_links,
            imported_links=imported_links,
            seen_this_run=seen_this_run,
            stats=stats,
        )
        appended = _append_rows(settings, group_rows, ctx)
        stats["rows_appended"] += appended

        save_checkpoint(
            {
                "step_opts": step_opts,
                "group_urls": group_urls,
                "next_index": idx + 1,
                "members_per_group": member_count,
                "mode": mode,
                "stats": stats,
                "paused_reason": None,
            }
        )

    clear_checkpoint()
    remove_attention_by_kind(COOKIE_ATTENTION_KIND)

    if stats["groups_failed"] and stats["groups_scraped"] == 0:
        return StepResult(
            status=StepStatus.FAILED,
            message="All group scrapes failed — check logs, cookies, and Apify token",
            stats=stats,
        )

    if stats["rows_appended"] == 0 and stats["members_fetched"] > 0:
        parts = []
        if stats["duplicate_imported"]:
            parts.append(f"{stats['duplicate_imported']} already in allimported")
        if stats["duplicate_dynamic"]:
            parts.append(f"{stats['duplicate_dynamic']} already on Dynamic Lead Sheet")
        if stats["duplicate_run"]:
            parts.append(f"{stats['duplicate_run']} duplicate in this scrape")
        if stats["no_link_skipped"]:
            parts.append(f"{stats['no_link_skipped']} without a Facebook link")
        detail = "; ".join(parts) if parts else "all members were filtered"
        return StepResult(
            status=StepStatus.SKIPPED,
            message=(
                f"Fetched {stats['members_fetched']} members — no new leads "
                f"({detail})"
            ),
            stats=stats,
        )

    if stats["groups_failed"] > 0 and stats["groups_scraped"] > 0:
        ctx.add_log(
            f"Step 1: {stats['groups_scraped']}/{stats['group_count']} group(s) "
            f"succeeded; {stats['groups_failed']} failed — check logs above"
        )

    if (
        stats["members_fetched"] < members_target
        and stats["groups_scraped"] == stats["group_count"]
        and stats["groups_scraped"] > 0
    ):
        ctx.add_log(
            f"Step 1: Apify returned {stats['members_fetched']}/{members_target} "
            "requested members (pagination may have stopped early)"
        )

    detail_parts = []
    if stats["duplicate_imported"]:
        detail_parts.append(f"{stats['duplicate_imported']} skipped (allimported)")
    if stats["duplicate_dynamic"]:
        detail_parts.append(f"{stats['duplicate_dynamic']} skipped (already on sheet)")
    if stats["no_link_skipped"]:
        detail_parts.append(f"{stats['no_link_skipped']} without link")
    detail_suffix = f" — {', '.join(detail_parts)}" if detail_parts else ""

    group_summary = f"{stats['groups_scraped']}/{stats['group_count']} group(s)"
    failure_suffix = ""
    if stats["groups_failed"]:
        failure_suffix = f" — {stats['groups_failed']} group scrape(s) failed"

    return StepResult(
        status=StepStatus.SUCCESS,
        message=(
            f"Appended {stats['rows_appended']} of {stats['members_fetched']} "
            f"fetched from {group_summary}{detail_suffix}{failure_suffix}"
        ),
        stats=stats,
    )
