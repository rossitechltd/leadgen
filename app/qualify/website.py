"""Live website checks for AI Qualify (Step 5)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

import httpx

from app.qualify.website_classifier import (
    WebsiteClassifyError,
    classify_website_html,
    extract_title,
    html_to_text,
)

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_FETCH_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

EXPIRED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"website\s+expired",
        r"this\s+account\s+has\s+expired",
        r"if\s+you\s+are\s+the\s+site\s+owner,\s*click\s+below\s+to\s+login",
        r"account\s+has\s+been\s+suspended",
        r"domain\s+has\s+expired",
        r"this\s+domain\s+is\s+for\s+sale",
        r"domain\s+expired",
        r"renew\s+this\s+domain",
        r"parked\s+free",
        r"sedoparking\.com",
        r"this\s+domain\s+is\s+parked",
        r"site\s+not\s+published",
        r"default\s+web\s+site\s+page",
        r"future\s+home\s+of\s+something\s+quite\s+cool",
        r"this\s+site\s+is\s+currently\s+unavailable",
        r"renew\s+your\s+(subscription|service|hosting)",
        r"subscription\s+has\s+expired",
        r"hosting\s+package\s+has\s+expired",
        r"suspend(?:ed|sion)?\s+notice",
        r"buy\s+this\s+domain",
    )
)

# Placeholder phrases — match visible page text only (not JSON/scripts in HTML).
_STRONG_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "new site is on its way",
    "site is on its way",
    "we're rebuilding",
    "were rebuilding",
    "in the meantime, get in touch",
    "get in touch — we'd love",
    "get in touch - we'd love",
)

_WEAK_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "under construction",
    "coming soon",
    "website is coming",
    "launching soon",
    "under maintenance",
    "site is being rebuilt",
    "new website coming",
    "website coming soon",
    "site coming soon",
    "watch this space",
)

_PLACEHOLDER_MAX_TEXT_LEN = 500

_SUBSTANTIAL_SITE_MIN_CHARS = 2000

# Strong signals of a live business site (used when AI returns unavailable)
_GENUINE_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcontact\s+us\b",
        r"\babout\s+us\b",
        r"\bour\s+services\b",
        r"\bservices\s+we\s+(offer|provide)\b",
        r"\bget\s+in\s+touch\b",
        r"\bbook\s+(now|online|an appointment)\b",
        r"\benroll(?:ment)?\b",
        r"\btuition\b",
        r"\b(private\s+lessons?|music\s+programs?)\b",
        r"\bmusic\s+tuition\b",
        r"\bmeet\s+the\s+team\b",
        r"\bprofessional\b",
        r"\bopening\s+hours\b",
        r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b",
        r"\b0\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}\b",
        r"\b(plumbing|electric|roofing|cleaning|salon|beauty|builders|construction|landscaping|decorat)",
    )
)

_SKIP_HOST_SUFFIXES = (
    "facebook.com",
    "fb.com",
    "instagram.com",
    "tiktok.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "google.com",
    "maps.app.goo.gl",
    "goo.gl",
)

_URL_IN_TEXT = re.compile(
    r"https?://[^\s<>\"'·•|]+",
    re.IGNORECASE,
)
_DOMAIN_TLD = r"(?:co\.uk|org\.uk|com|org|net|uk|io|biz)"
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SINGLE_DOMAIN = re.compile(
    rf"^(?:www\.)?({_DOMAIN_LABEL}\.{_DOMAIN_TLD})$",
    re.IGNORECASE,
)
_DOMAIN_IN_TEXT = re.compile(
    rf"(?:^|[\s\n·•|,;])(?:www\.)?({_DOMAIN_LABEL}\.{_DOMAIN_TLD})(?=[\s/<>\"'·•|,;]|$)",
    re.IGNORECASE,
)
_DOMAIN_SEPARATORS = re.compile(r"\s*[·•|,;]\s*|\s+and\s+", re.IGNORECASE)


def _clean_host_token(raw: str) -> str:
    token = (raw or "").strip().strip("'\"")
    token = re.sub(r"^https?://", "", token, flags=re.IGNORECASE)
    token = token.split("/")[0].split("?")[0].strip()
    if token.startswith("www."):
        token = token[4:]
    return token.lower()


def _is_valid_hostname(host: str) -> bool:
    if not host:
        return False
    if any(ch in host for ch in (" ", "·", "•", "|", "/", "\\", "@")):
        return False
    return _SINGLE_DOMAIN.match(host) is not None


def _domain_tokens_from_text(raw: str) -> list[str]:
    """Split combined values like 'a.com · b.com' into individual hostnames."""
    tokens: list[str] = []
    seen: set[str] = set()
    for chunk in _DOMAIN_SEPARATORS.split((raw or "").strip()):
        host = _clean_host_token(chunk)
        if host and host not in seen and _is_valid_hostname(host):
            seen.add(host)
            tokens.append(host)
    if not tokens and raw.strip():
        host = _clean_host_token(raw)
        if _is_valid_hostname(host):
            tokens.append(host)
    return tokens


class WebsiteStatus(str, Enum):
    NO_URL = "no_url"
    EXPIRED = "expired"
    GENUINE = "genuine"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class WebsiteCheckResult:
    status: WebsiteStatus
    url: str = ""
    detail: str = ""
    page_phones: tuple[str, ...] = ()


def normalize_website_url(raw: str) -> str:
    for host in _domain_tokens_from_text(raw):
        return f"https://{host}"
    return ""


def _host_allowed(host: str) -> bool:
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    for suffix in _SKIP_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return False
    if host == "google.com" or host.endswith(".google.com"):
        return "/maps" in host  # only block maps paths handled at url level
    return True


def is_trackable_website(raw: str) -> bool:
    url = normalize_website_url(raw)
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not _host_allowed(host):
            return False
        if "google." in host and "/maps" in (parsed.path or "").lower():
            return False
        return bool(host)
    except Exception:
        return False


def scrape_confirms_business_website(scrape_text: str, website_url: str) -> bool:
    """
    Facebook scrape lists the domain in Links with business page signals.

    Used only when HTTP fetch fails — must not fire on follower counts alone.
    """
    if not scrape_text.strip() or not website_url:
        return False
    try:
        host = (urlparse(website_url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return False
    if not host or host not in scrape_text.lower():
        return False

    lower = scrape_text.lower()
    if any(phrase in lower for phrase in _STRONG_PLACEHOLDER_PHRASES):
        return False
    if "rebuilding" in lower and "on its way" in lower:
        return False

    # Links section + domain + long scrape suggests a real site (not just FB stats).
    if "links" in lower and host in lower and len(scrape_text) > 1200:
        return True

    return False


def discover_website_url(
    *,
    website_link: str = "",
    refined_text: str = "",
    scrape_text: str = "",
) -> str:
    """Pick the best business website URL from sheet columns and scrape text."""
    urls = discover_website_urls(
        website_link=website_link,
        refined_text=refined_text,
        scrape_text=scrape_text,
    )
    return urls[0] if urls else ""


def discover_website_urls(
    *,
    website_link: str = "",
    refined_text: str = "",
    scrape_text: str = "",
) -> list[str]:
    """All trackable business website URLs found for a lead."""
    candidates: list[str] = []

    if website_link.strip():
        for host in _domain_tokens_from_text(website_link):
            candidates.append(host)
        if not _domain_tokens_from_text(website_link):
            candidates.append(website_link.strip())

    for text in (refined_text, scrape_text):
        if not text:
            continue
        for match in _URL_IN_TEXT.finditer(text):
            candidates.append(match.group(0).rstrip(".,);"))
        for match in _DOMAIN_IN_TEXT.finditer(text):
            candidates.append(match.group(1))

    urls: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        url = normalize_website_url(raw)
        key = url.lower()
        if not url or key in seen:
            continue
        seen.add(key)
        if is_trackable_website(url):
            urls.append(url)
    return urls


def is_connection_fetch_failure(detail: str) -> bool:
    """True when HTTP fetch failed before a response (timeout, DNS, etc.)."""
    lowered = (detail or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("http "):
        return False
    return True


def _looks_expired(html: str, text: str) -> bool:
    combined = html + " " + text
    for pattern in EXPIRED_PATTERNS:
        if pattern.search(combined):
            return True
    lowered = combined.lower()
    if "account has expired" in lowered or "website expired" in lowered:
        return True
    if "site owner" in lowered and "log in" in lowered and "click below" in lowered:
        return True

    text_lower = _normalize_html_entities(text).lower()
    stripped_len = len(text.strip())
    if stripped_len < _PLACEHOLDER_MAX_TEXT_LEN:
        for phrase in _WEAK_PLACEHOLDER_PHRASES + _STRONG_PLACEHOLDER_PHRASES:
            if phrase in text_lower:
                return True
        if "rebuilding" in text_lower or "on its way" in text_lower:
            return True

    return False


def _looks_substantial_business_site(text: str, title: str) -> bool:
    """Large public sites with rich program/service content are active business websites."""
    stripped = text.strip()
    if len(stripped) < _SUBSTANTIAL_SITE_MIN_CHARS:
        return False
    combined = _normalize_html_entities(f"{title} {text}").lower()
    markers = (
        "program",
        "lesson",
        "tuition",
        "enroll",
        "service",
        "about",
        "contact",
        "portfolio",
        "pricing",
        "booking",
        "academy",
        "private lesson",
        "performance",
        "teaching",
        "teacher",
        "workshop",
        "schools",
    )
    hits = sum(1 for marker in markers if marker in combined)
    return hits >= 3


def _ai_expired_reason(reason: str) -> bool:
    """Only treat AI expired when reason cites hosting expiry, not HTTP forbidden."""
    r = (reason or "").lower()
    if "forbidden" in r or "403" in r or "401" in r:
        return False
    expired_phrases = (
        "website expired",
        "account has expired",
        "domain expired",
        "domain parked",
        "for sale",
        "hosting expired",
        "subscription has expired",
        "site owner",
        "parked",
    )
    return any(p in r for p in expired_phrases)


def _is_live_bot_blocked(status_code: int, html: str, text: str) -> bool:
    """HTTP 401/403 with a real response body — site exists but blocks bots (not hosting expiry)."""
    if status_code not in (401, 403):
        return False
    if _looks_expired(html, text):
        return False
    return len(html) > 800 or len(text) > 100


def _normalize_html_entities(text: str) -> str:
    return (
        text.replace("&rsquo;", "'")
        .replace("&lsquo;", "'")
        .replace("&mdash;", "—")
        .replace("&ndash;", "-")
        .replace("&amp;", "&")
    )


def _looks_placeholder(html: str, text: str) -> bool:
    """
    Placeholder / under-construction pages are NOT active business sites.

    Uses visible text only — ignores CMS JSON in raw HTML (e.g. Squarespace
    "Coming Soon" collection metadata).
    """
    if _looks_substantial_business_site(text, extract_title(html)):
        return False

    text_lower = _normalize_html_entities(text).lower()
    stripped_len = len(text.strip())

    for phrase in _STRONG_PLACEHOLDER_PHRASES:
        if phrase in text_lower:
            return True

    if stripped_len >= _PLACEHOLDER_MAX_TEXT_LEN:
        return False

    for phrase in _WEAK_PLACEHOLDER_PHRASES:
        if phrase in text_lower:
            return True
    if "rebuilding" in text_lower or "on its way" in text_lower:
        return True

    return False


def _looks_genuine_heuristic(text: str, title: str, html: str) -> bool:
    """
    Fallback when AI is unsure — detect real business sites we should reject.

    Requires substantial content and multiple business signals, and no expired text.
    """
    if _looks_placeholder(html, text):
        return False

    if _looks_expired(html, text):
        return False

    if _looks_substantial_business_site(text, title):
        return True

    combined = f"{title} {text}"
    stripped_len = len(text.strip())
    if stripped_len < 120:
        return False

    signals = sum(1 for pattern in _GENUINE_SIGNAL_PATTERNS if pattern.search(combined))
    if signals >= 3:
        return True
    if signals >= 2 and stripped_len >= 120:
        return True
    if signals >= 1 and stripped_len >= 400:
        return True

    # Meta description often present on real sites
    meta = re.search(
        r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)[\"']",
        html,
        re.IGNORECASE,
    )
    if meta and len(meta.group(1).strip()) > 40 and signals >= 1:
        return True

    return False


def _live_fetch_suggests_business_site(
    status_code: int,
    text: str,
    title: str,
    html: str,
) -> bool:
    """
    Successful HTTP fetch with real page content — treat as active business site.

    Used when AI mislabels a live site as expired/unavailable (qualify should remove).
    """
    if status_code >= 400:
        return False
    if _looks_expired(html, text) or _looks_placeholder(html, text):
        return False
    if _looks_genuine_heuristic(text, title, html):
        return True
    if _looks_substantial_business_site(text, title):
        return True

    stripped = text.strip()
    if len(stripped) < 200:
        return False

    business_hits = _readable_business_word_count(stripped)
    if business_hits >= 2:
        return True
    if business_hits >= 1 and len(stripped) >= 500:
        return True
    if title.strip() and len(title.strip()) > 3 and len(stripped) >= 350:
        return True
    return False


def _extract_page_phones(text: str) -> list[str]:
    from app.refinement.phones import normalize_uk_phone

    phones: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"(?:\+?44[\s(]*(?:0)?\s*|\+?44\s*|0)\d[\d\s]{8,14}",
        text or "",
        re.IGNORECASE,
    ):
        normalized = normalize_uk_phone(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            phones.append(normalized)
    return phones


def _result_with_phones(
    result: WebsiteCheckResult, *text_sources: str
) -> WebsiteCheckResult:
    phones: list[str] = []
    seen: set[str] = set()
    for source in text_sources:
        for phone in _extract_page_phones(source):
            if phone not in seen:
                seen.add(phone)
                phones.append(phone)
    if not phones:
        return result
    return WebsiteCheckResult(
        status=result.status,
        url=result.url,
        detail=result.detail,
        page_phones=tuple(phones),
    )


def _fallback_page_text(
    scrape_text: str,
    refined_text: str,
    business_name: str,
) -> str:
    parts: list[str] = []
    if business_name.strip():
        parts.append(f"Business: {business_name.strip()}")
    if refined_text.strip():
        parts.append(refined_text.strip())
    if scrape_text.strip():
        parts.append(scrape_text.strip())
    return "\n\n".join(parts)


_READABLE_BUSINESS_VOCAB: tuple[str, ...] = (
    "about",
    "contact",
    "service",
    "lesson",
    "tuition",
    "music",
    "teach",
    "phone",
    "email",
    "welcome",
    "team",
    "professional",
    "enroll",
    "program",
)


def _readable_business_word_count(text: str) -> int:
    lower = _normalize_html_entities(text).lower()
    return sum(1 for word in _READABLE_BUSINESS_VOCAB if word in lower)


def _fetch_is_unreadable(text: str, html: str) -> bool:
    """True when HTTP succeeded but body is JS/CSS shell (e.g. Wix) not readable content."""
    stripped = text.strip()
    if len(stripped) < 80:
        return True

    business_hits = _readable_business_word_count(stripped)
    word_count = len(re.findall(r"\b[a-z]{3,}\b", stripped.lower()))

    if len(stripped) > 800 and business_hits < 2:
        return True
    if stripped.count("{") > 15 and business_hits < 2:
        return True
    if "wixstatic.com" in html.lower() and business_hits < 3:
        return True
    if len(stripped) > 2000 and word_count > 200 and business_hits < 2:
        return True

    return False


def _classify_from_content(
    url: str,
    business_name: str,
    content: str,
    *,
    timeout: float,
    api_key: str,
    model: str,
    base_url: str,
    source_label: str,
) -> WebsiteCheckResult:
    """Classify using scrape/refined text when live HTML fetch is not readable."""
    title = business_name.strip() or extract_title(content)
    text = content.strip()
    if not text:
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail="no readable website or scrape content",
        )

    if _looks_substantial_business_site(text, title):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail=f"substantial business content ({source_label})",
            ),
            text,
        )

    if _looks_placeholder("", text):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.EXPIRED,
                url=url,
                detail=f"placeholder signals in {source_label}",
            ),
            text,
        )

    if _looks_genuine_heuristic(text, title, ""):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail=f"business website signals in {source_label}",
            ),
            text,
        )

    stripped = text.strip()
    if len(stripped) >= 600 and _readable_business_word_count(stripped) >= 3:
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail=f"rich business scrape/refined content ({source_label})",
            ),
            text,
        )

    if api_key:
        try:
            ai = classify_website_html(
                url=url,
                page_title=title,
                page_text=text[:8000],
                business_name=business_name,
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
        except WebsiteClassifyError as exc:
            return _result_with_phones(
                WebsiteCheckResult(
                    status=WebsiteStatus.UNREACHABLE,
                    url=url,
                    detail=f"{source_label} classify failed: {exc}",
                ),
                text,
            )

        if ai.status == "genuine_active":
            return _result_with_phones(
                WebsiteCheckResult(
                    status=WebsiteStatus.GENUINE,
                    url=url,
                    detail=ai.reason or f"AI genuine ({source_label})",
                ),
                text,
            )
        if ai.status == "expired_or_parked":
            return _result_with_phones(
                WebsiteCheckResult(
                    status=WebsiteStatus.EXPIRED,
                    url=url,
                    detail=ai.reason or f"AI expired ({source_label})",
                ),
                text,
            )

    return _result_with_phones(
        WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=f"no active website detected in {source_label}",
        ),
        text,
    )


def _fetch_html(url: str, *, timeout: float) -> tuple[int, str]:
    last_error: str | None = None
    for attempt_url in (url, url.replace("https://", "http://", 1) if url.startswith("https://") else url):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers=_FETCH_HEADERS,
            ) as client:
                response = client.get(attempt_url)
                body = response.text[:200_000] if response.text else ""
                return response.status_code, body
        except httpx.HTTPError as exc:
            last_error = str(exc)
            logger.info("Fetch failed %s: %s", attempt_url, exc)
    raise httpx.HTTPError(last_error or "fetch failed")


def check_website(
    raw_url: str,
    *,
    timeout: float = 15.0,
    business_name: str = "",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    scrape_text: str = "",
    refined_text: str = "",
) -> WebsiteCheckResult:
    if not is_trackable_website(raw_url):
        return WebsiteCheckResult(status=WebsiteStatus.NO_URL, detail="no website url")

    url = normalize_website_url(raw_url)
    fallback_content = _fallback_page_text(scrape_text, refined_text, business_name)

    try:
        status_code, body = _fetch_html(url, timeout=timeout)
    except httpx.HTTPError as exc:
        if fallback_content.strip():
            return _classify_from_content(
                url,
                business_name,
                fallback_content,
                timeout=timeout,
                api_key=api_key,
                model=model,
                base_url=base_url,
                source_label="scrape/refined (fetch failed)",
            )
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=str(exc),
        )

    if status_code >= 500:
        if fallback_content.strip():
            return _classify_from_content(
                url,
                business_name,
                fallback_content,
                timeout=timeout,
                api_key=api_key,
                model=model,
                base_url=base_url,
                source_label="scrape/refined (HTTP error)",
            )
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=f"HTTP {status_code}",
        )

    title = extract_title(body)
    text = html_to_text(body)

    if _fetch_is_unreadable(text, body) and fallback_content.strip():
        return _classify_from_content(
            url,
            business_name,
            fallback_content,
            timeout=timeout,
            api_key=api_key,
            model=model,
            base_url=base_url,
            source_label="scrape/refined (JS site)",
        )

    if _looks_expired(body, text):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.EXPIRED,
                url=url,
                detail="expired or parked hosting page",
            ),
            text,
        )

    if _looks_substantial_business_site(text, title):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail="substantial business website (rich public content)",
            ),
            text,
        )

    if _looks_placeholder(body, text):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.EXPIRED,
                url=url,
                detail="placeholder or under-construction site (keep lead)",
            ),
            text,
        )

    if status_code == 404 or (status_code >= 400 and len(text) < 80):
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=f"HTTP {status_code}",
        )

    if _is_live_bot_blocked(status_code, body, text):
        return WebsiteCheckResult(
            status=WebsiteStatus.GENUINE,
            url=url,
            detail=f"active website (HTTP {status_code}, blocks bots but site is live)",
        )

    if not api_key:
        if _looks_genuine_heuristic(text, title, body):
            return WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail="heuristic: active business website",
            )
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail="no AI key for website classification",
        )

    try:
        ai = classify_website_html(
            url=url,
            page_title=title,
            page_text=text,
            business_name=business_name,
            api_key=api_key,
            model=model,
            base_url=base_url,
        )
    except WebsiteClassifyError as exc:
        logger.warning("Website AI classify failed for %s: %s", url, exc)
        if _looks_genuine_heuristic(text, title, body):
            return WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail="heuristic fallback after AI error",
            )
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=f"classification failed: {exc}",
        )

    # Expired text always wins — keep lead even if AI mislabels
    if _looks_expired(body, text):
        return WebsiteCheckResult(
            status=WebsiteStatus.EXPIRED,
            url=url,
            detail="expired hosting page (text match overrides AI)",
        )

    if ai.status == "genuine_active":
        return WebsiteCheckResult(
            status=WebsiteStatus.GENUINE,
            url=url,
            detail=ai.reason or "AI: genuine active website",
        )

    if _looks_substantial_business_site(text, title):
        return WebsiteCheckResult(
            status=WebsiteStatus.GENUINE,
            url=url,
            detail="substantial business website (rich public content)",
        )

    if ai.status == "expired_or_parked":
        if _looks_expired(body, text) or _ai_expired_reason(ai.reason) or _looks_placeholder(body, text):
            return WebsiteCheckResult(
                status=WebsiteStatus.EXPIRED,
                url=url,
                detail=ai.reason or "AI: expired or parked",
            )
        if _is_live_bot_blocked(status_code, body, text):
            return WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail="active website (bot-blocked; AI misread as expired)",
            )
        if _live_fetch_suggests_business_site(status_code, text, title, body):
            return _result_with_phones(
                WebsiteCheckResult(
                    status=WebsiteStatus.GENUINE,
                    url=url,
                    detail="active business website (live fetch; AI misread as expired)",
                ),
                text,
            )
        return WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=ai.reason or "not expired hosting",
        )

    if _live_fetch_suggests_business_site(status_code, text, title, body):
        return _result_with_phones(
            WebsiteCheckResult(
                status=WebsiteStatus.GENUINE,
                url=url,
                detail="active business website (live fetch content)",
            ),
            text,
        )

    if _looks_genuine_heuristic(text, title, body):
        return WebsiteCheckResult(
            status=WebsiteStatus.GENUINE,
            url=url,
            detail="heuristic: active business website (AI unavailable)",
        )

    unreachable = _result_with_phones(
        WebsiteCheckResult(
            status=WebsiteStatus.UNREACHABLE,
            url=url,
            detail=ai.reason or "AI: unavailable",
        ),
        text,
    )
    if fallback_content.strip() and (
        _fetch_is_unreadable(text, body)
        or unreachable.status == WebsiteStatus.UNREACHABLE
    ):
        return _classify_from_content(
            url,
            business_name,
            fallback_content,
            timeout=timeout,
            api_key=api_key,
            model=model,
            base_url=base_url,
            source_label="scrape/refined (HTML inconclusive)",
        )
    return unreachable
