"""Apify Facebook Group Members Scraper integration."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

logger = logging.getLogger(__name__)

DEFAULT_ACTOR_ID = "curious_coder/facebook-group-member-scraper"
REQUIRED_COOKIE_NAMES = ("c_user", "xs")
# Volatile / device-specific — stale values often break remote sessions.
EXCLUDED_COOKIE_NAMES = frozenset({"presence", "wd"})
# Seconds to wait between paginated Apify runs (reduces session invalidation).
PAGINATION_PAUSE_SECS = 20


class ApifyConfigError(Exception):
    """Missing or invalid Apify / Facebook cookie configuration."""


def is_cookie_related_error(exc: BaseException) -> bool:
    """True when Facebook session cookies are missing or rejected."""
    if isinstance(exc, ApifyConfigError):
        return True
    message = str(exc).lower()
    if "cookie" in message:
        return True
    if "c_user" in message or "session" in message and "invalid" in message:
        return True
    if "cookies are invalid" in message or "cookies are no longer valid" in message:
        return True
    return False


def normalize_cookies_for_actor(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Cookie-Editor export to Playwright-compatible cookie objects."""
    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        name = cookie["name"]
        if name in EXCLUDED_COOKIE_NAMES:
            continue
        value = str(cookie["value"])
        if "%" in value:
            value = unquote(value)
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": cookie.get("domain", ".facebook.com"),
            "path": cookie.get("path", "/"),
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        if cookie.get("expirationDate") is not None:
            item["expires"] = int(cookie["expirationDate"])
        elif cookie.get("expires") is not None:
            item["expires"] = int(cookie["expires"])
        same_site = cookie.get("sameSite")
        if same_site == "no_restriction":
            item["sameSite"] = "None"
        elif same_site:
            item["sameSite"] = same_site
        normalized.append(item)
    return normalized


def _extract_cookies_array(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and "cookies" in raw:
        cookies = raw["cookies"]
    elif isinstance(raw, list):
        cookies = raw
    else:
        raise ApifyConfigError(
            "Cookies must be a JSON array (Cookie-Editor export) "
            "or an object with a 'cookies' array."
        )
    if not isinstance(cookies, list):
        raise ApifyConfigError("Cookies must be a JSON array.")
    if not cookies:
        raise ApifyConfigError("Cookies array is empty.")
    for index, cookie in enumerate(cookies):
        if not isinstance(cookie, dict):
            raise ApifyConfigError(f"Cookie at index {index} must be an object.")
        if "name" not in cookie or "value" not in cookie:
            raise ApifyConfigError(
                f"Cookie at index {index} must include 'name' and 'value'."
            )
    return cookies


def _validate_session_cookie_names(cookies: list[dict[str, Any]]) -> None:
    normalized = normalize_cookies_for_actor(cookies)
    names = {cookie["name"] for cookie in normalized}
    missing = [name for name in REQUIRED_COOKIE_NAMES if name not in names]
    if missing:
        raise ApifyConfigError(
            f"Facebook cookies missing required session keys: {', '.join(missing)}. "
            "Re-export cookies while logged into facebook.com."
        )


def parse_cookies_json(text: str) -> list[dict[str, Any]]:
    """Parse pasted Cookie-Editor JSON and validate Facebook session keys."""
    stripped = (text or "").strip()
    if not stripped:
        raise ApifyConfigError("Paste Cookie-Editor JSON export.")
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ApifyConfigError(f"Invalid JSON: {exc}") from exc
    cookies = _extract_cookies_array(raw)
    _validate_session_cookie_names(cookies)
    return cookies


def write_cookies_file(cookies_path: Path, cookies: list[dict[str, Any]]) -> None:
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path.write_text(
        json.dumps(cookies, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cookies_file_status(cookies_path: Path) -> dict[str, Any]:
    status: dict[str, Any] = {
        "configured": False,
        "path": str(cookies_path),
        "cookie_count": 0,
        "has_c_user": False,
        "has_xs": False,
        "updated_at": None,
        "error": None,
    }
    if not cookies_path.exists():
        status["error"] = "File not found"
        return status
    try:
        raw = json.loads(cookies_path.read_text(encoding="utf-8"))
        cookies = _extract_cookies_array(raw)
        normalized = normalize_cookies_for_actor(cookies)
        names = {cookie["name"] for cookie in normalized}
        status["cookie_count"] = len(cookies)
        status["has_c_user"] = "c_user" in names
        status["has_xs"] = "xs" in names
        status["configured"] = status["has_c_user"] and status["has_xs"]
        status["updated_at"] = cookies_path.stat().st_mtime
        if not status["configured"]:
            missing = [
                name for name in REQUIRED_COOKIE_NAMES if name not in names
            ]
            status["error"] = f"Missing session keys: {', '.join(missing)}"
    except (ApifyConfigError, json.JSONDecodeError, OSError) as exc:
        status["error"] = str(exc)
    return status


def load_cookies(cookies_path: Path) -> list[dict[str, Any]]:
    if not cookies_path.exists():
        raise ApifyConfigError(
            f"Facebook cookies file not found: {cookies_path}\n"
            "Export facebook.com cookies with Cookie-Editor and save to this path."
        )
    raw = json.loads(cookies_path.read_text(encoding="utf-8"))
    cookies = _extract_cookies_array(raw)
    _validate_session_cookie_names(cookies)
    normalized = normalize_cookies_for_actor(cookies)
    logger.info("Loaded %s Facebook cookies from %s", len(normalized), cookies_path)
    return normalized


def load_group_urls(groups_path: Path) -> list[str]:
    from app.scrapers.facebook_groups import load_group_urls as _load

    return _load(groups_path)


def build_proxy_strategies(
    proxy_country: str,
    proxy_groups: tuple[str, ...] | list[str],
) -> list[dict[str, Any]]:
    """Prefer residential proxy in the login country — best for Facebook cookie sessions."""
    strategies: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(label: str, proxy: dict[str, Any]) -> None:
        if label in seen:
            return
        seen.add(label)
        strategies.append({"label": label, "proxy": proxy})

    add(
        f"residential:{proxy_country}",
        {
            "useApifyProxy": True,
            "apifyProxyCountry": proxy_country,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
    )
    for group in proxy_groups:
        group_upper = group.upper()
        if group_upper == "RESIDENTIAL":
            continue
        add(
            f"{group.lower()}:{proxy_country}",
            {
                "useApifyProxy": True,
                "apifyProxyCountry": proxy_country,
                "apifyProxyGroups": [group_upper],
            },
        )
    add("datacenter:any", {"useApifyProxy": True})
    add(
        f"datacenter:{proxy_country}",
        {"useApifyProxy": True, "apifyProxyCountry": proxy_country},
    )
    if proxy_country.upper() != "US":
        add(
            "datacenter:US",
            {"useApifyProxy": True, "apifyProxyCountry": "US"},
        )
    return strategies


def build_actor_input(
    *,
    cookies: list[dict[str, Any]],
    group_url: str,
    min_delay: int,
    max_delay: int,
    proxy: dict[str, Any],
    count: int | None,
    cursor: str | None,
) -> dict[str, Any]:
    if min_delay < 0:
        min_delay = 0
    if max_delay < 10:
        logger.warning("Apify requires maxDelay >= 10; raising %s -> 10", max_delay)
        max_delay = 10

    payload: dict[str, Any] = {
        "cookies": cookies,
        "scrapeGroupMembers.groupUrl": group_url,
        "minDelay": min_delay,
        "maxDelay": max_delay,
        "proxy": proxy,
    }
    if count is not None and count > 0:
        payload["count"] = count
    if cursor:
        payload["cursor"] = cursor
    return payload


def _normalize_run(run: Any) -> dict[str, Any]:
    if run is None:
        return {}
    if isinstance(run, dict):
        return run
    status = getattr(run, "status", None)
    status_value = status.value if hasattr(status, "value") else status
    return {
        "id": getattr(run, "id", None),
        "status": str(status_value) if status_value is not None else None,
        "statusMessage": getattr(run, "status_message", None),
        "defaultDatasetId": getattr(run, "default_dataset_id", None),
    }


def _tail_log_lines(log_text: str | None, limit: int = 15) -> list[str]:
    if not log_text:
        return []
    lines = [line for line in log_text.splitlines() if line.strip()]
    return lines[-limit:]


def _proxy_hint_from_log(log_text: str | None) -> str | None:
    if not log_text:
        return None
    if "ProxyAuthRequired" in log_text:
        return (
            "Actor could not authenticate to Apify Proxy for this proxy mode. "
            "The app will retry datacenter/residential automatically. "
            "If all fail, test the same input in Apify Console and compare proxy settings."
        )
    return None


def _format_run_error(
    group_url: str,
    run_data: dict[str, Any],
    proxy_label: str,
    log_tail: list[str] | None = None,
) -> str:
    if not run_data:
        return f"Apify run returned no result for {group_url}"
    status = run_data.get("status", "UNKNOWN")
    run_id = run_data.get("id", "?")
    status_message = run_data.get("statusMessage") or ""
    console_url = f"https://console.apify.com/actors/runs/{run_id}"
    parts = [
        f"Apify run failed for {group_url}",
        f"proxy_mode={proxy_label}",
        f"status={status}",
        f"run_id={run_id}",
    ]
    if status_message:
        parts.append(f"message={status_message}")
    if log_tail:
        proxy_hint = _proxy_hint_from_log("\n".join(log_tail))
        if proxy_hint:
            parts.append(proxy_hint)
        parts.append("log_tail=" + " | ".join(log_tail[-5:]))
    parts.append(f"console={console_url}")
    return " | ".join(parts)


def _member_key(member: dict[str, Any]) -> str:
    for field in ("userId", "groupMemberId", "profileUrl", "url"):
        value = member.get(field)
        if value:
            return str(value)
    return str(member.get("name") or id(member))


def _dedupe_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for member in members:
        key = _member_key(member)
        if key in seen:
            continue
        seen.add(key)
        unique.append(member)
    return unique


def _get_run_resume_cursor(client: ApifyClient, run_id: str | None) -> str | None:
    if not run_id:
        return None
    try:
        run_obj = client.run(run_id).get()
        kvs_id = getattr(run_obj, "default_key_value_store_id", None)
        if not kvs_id:
            return None
        kvs = client.key_value_store(kvs_id)
        for key in ("lastCursor", "state"):
            rec = kvs.get_record(key)
            value = rec.get("value") if isinstance(rec, dict) else getattr(rec, "value", None)
            if isinstance(value, dict):
                cursor = value.get("lastCursor") or value.get("cursor")
                if cursor:
                    return str(cursor)
            elif isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.warning("Could not read Apify resume cursor for run %s: %s", run_id, exc)
    return None


def _load_run_items(
    client: ApifyClient,
    run_data: dict[str, Any],
    *,
    group_url: str,
    cookies_path: Path,
) -> list[dict[str, Any]]:
    dataset_id = run_data.get("defaultDatasetId")
    if not dataset_id:
        return []
    items = list(client.dataset(dataset_id).iterate_items())
    actor_error = _actor_error_from_items(items)
    if actor_error:
        error_lower = actor_error.lower()
        if "cookie" in error_lower:
            raise RuntimeError(
                "Facebook cookies are invalid or expired. Log into facebook.com in your browser, "
                f"re-export cookies with Cookie-Editor to {cookies_path.name}, then run Step 1 again. "
                f"Actor message: {actor_error}"
            )
        raise RuntimeError(f"Apify actor error: {actor_error}")
    logger.info("Apify returned %s member(s) for %s", len(items), group_url)
    return items


def _actor_error_from_items(items: list[dict[str, Any]]) -> str | None:
    if not items:
        return None
    if len(items) == 1 and items[0].get("error"):
        return str(items[0]["error"])
    errors = [str(item["error"]) for item in items if item.get("error")]
    if errors and len(errors) == len(items):
        return errors[0]
    return None


def _run_actor_once(
    client: ApifyClient,
    *,
    actor_id: str,
    run_input: dict[str, Any],
    proxy_label: str,
    timeout_secs: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    logger.info("Trying Apify proxy mode '%s': %s", proxy_label, run_input["proxy"])
    try:
        run = client.actor(actor_id).call(
            run_input=run_input,
            wait_duration=timedelta(seconds=timeout_secs),
        )
    except ApifyApiError as exc:
        message = str(exc)
        if "approvePermissions" in message or "approve its permissions" in message.lower():
            raise RuntimeError(
                "Apify actor permissions not approved. Open this link once, click Approve, "
                "then run Step 1 again: https://console.apify.com/actors/y1bjlVpn2Kkdd2mi7?approvePermissions=true"
            ) from exc
        raise RuntimeError(f"Apify API error ({proxy_label}): {message}") from exc

    run_data = _normalize_run(run)
    status = run_data.get("status")
    run_id = run_data.get("id")

    if not run_data or status != "SUCCEEDED":
        log_tail: list[str] = []
        if run_id:
            try:
                log_text = client.run(run_id).log().get()
                log_tail = _tail_log_lines(log_text)
                for line in log_tail:
                    logger.error("[apify run %s] %s", run_id, line)
            except Exception as exc:
                logger.warning("Could not fetch Apify run log for %s: %s", run_id, exc)
        error = _format_run_error("", run_data, proxy_label, log_tail)
        logger.warning("%s", error)
        return run_data, log_tail

    return run_data, []


def _scrape_pages_with_strategy(
    client: ApifyClient,
    *,
    actor_id: str,
    cookies: list[dict[str, Any]],
    cookies_path: Path,
    group_url: str,
    min_delay: int,
    max_delay: int,
    proxy: dict[str, Any],
    proxy_label: str,
    target_count: int | None,
    start_cursor: str | None,
    timeout_secs: int,
) -> list[dict[str, Any]] | None:
    """Run one or more Apify pages until target_count is reached. None if the run failed."""
    collected: list[dict[str, Any]] = []
    resume_cursor = start_cursor
    max_pages = 10
    if target_count:
        max_pages = min(10, max(3, (target_count // 5) + 2))

    for page in range(max_pages):
        if target_count is not None and len(collected) >= target_count:
            return collected[:target_count]

        page_count = target_count - len(collected) if target_count else None
        if page_count is not None and page_count <= 0:
            break

        run_input = build_actor_input(
            cookies=cookies,
            group_url=group_url,
            min_delay=min_delay,
            max_delay=max_delay,
            proxy=proxy,
            count=page_count,
            cursor=resume_cursor,
        )
        run_data, log_tail = _run_actor_once(
            client,
            actor_id=actor_id,
            run_input=run_input,
            proxy_label=proxy_label,
            timeout_secs=timeout_secs,
        )

        status = run_data.get("status")
        if status != "SUCCEEDED":
            if collected:
                logger.warning(
                    "Apify page %s failed after collecting %s members; returning partial results",
                    page + 1,
                    len(collected),
                )
                return collected[:target_count] if target_count else collected
            return None

        run_id = run_data.get("id")
        logger.info(
            "Apify page %s succeeded with proxy mode '%s': run_id=%s",
            page + 1,
            proxy_label,
            run_id,
        )
        items = _load_run_items(client, run_data, group_url=group_url, cookies_path=cookies_path)
        collected = _dedupe_members(collected + items)

        if target_count is not None and len(collected) >= target_count:
            return collected[:target_count]

        if target_count is None and not items:
            break

        new_cursor = _get_run_resume_cursor(client, run_id)
        if target_count is not None and len(collected) < target_count:
            if not new_cursor:
                logger.warning(
                    "Apify returned %s/%s members and no pagination cursor — "
                    "check lastCursor in run storage (run_id=%s)",
                    len(collected),
                    target_count,
                    run_id,
                )
                break
            if not items:
                break
        elif not new_cursor or not items:
            break
        if new_cursor == resume_cursor and page > 0:
            break

        resume_cursor = new_cursor
        logger.info(
            "Continuing Apify scrape from cursor (have %s/%s members); "
            "pausing %ss to protect Facebook session",
            len(collected),
            target_count or "all",
            PAGINATION_PAUSE_SECS,
        )
        time.sleep(PAGINATION_PAUSE_SECS)

    if target_count is not None:
        return collected[:target_count]
    return collected


def scrape_group_members(
    *,
    api_token: str,
    cookies_path: Path,
    group_url: str,
    actor_id: str = DEFAULT_ACTOR_ID,
    min_delay: int = 1,
    max_delay: int = 10,
    proxy_country: str = "GB",
    proxy_groups: tuple[str, ...] | list[str] = (),
    count: int | None = None,
    cursor: str | None = None,
    timeout_secs: int = 3600,
) -> list[dict[str, Any]]:
    cookies = load_cookies(cookies_path)
    client = ApifyClient(api_token)
    strategies = build_proxy_strategies(proxy_country, proxy_groups)

    logger.info(
        "Starting Apify actor %s for group %s (count=%s, delay=%s-%ss, %s proxy modes)",
        actor_id,
        group_url,
        count or "all",
        min_delay,
        max_delay,
        len(strategies),
    )

    last_error = "All proxy modes failed"
    for strategy in strategies:
        members = _scrape_pages_with_strategy(
            client,
            actor_id=actor_id,
            cookies=cookies,
            cookies_path=cookies_path,
            group_url=group_url,
            min_delay=min_delay,
            max_delay=max_delay,
            proxy=strategy["proxy"],
            proxy_label=strategy["label"],
            target_count=count,
            start_cursor=cursor,
            timeout_secs=timeout_secs,
        )
        if members is None:
            last_error = f"Apify run failed for {group_url} (proxy_mode={strategy['label']})"
            if strategy["label"] != "datacenter:any":
                continue
            break

        if members:
            logger.info(
                "Apify collected %s member(s) for %s using '%s' | newest=%s",
                len(members),
                group_url,
                strategy["label"],
                members[0].get("name"),
            )
            if count and len(members) < count:
                logger.warning(
                    "Apify returned %s/%s requested members for %s",
                    len(members),
                    count,
                    group_url,
                )
            return members

        last_error = f"Apify returned no members for {group_url} (proxy_mode={strategy['label']})"

    raise RuntimeError(last_error)
