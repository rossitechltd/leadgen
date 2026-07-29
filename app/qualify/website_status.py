"""Deterministic website status classification for lead qualification."""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import httpx

from app.qualify.website_classifier import html_to_text, extract_title
from app.qualify.website_status_platforms import (
    DIRECTORY_PLATFORM_HOSTS,
    DOMAIN_MARKETPLACE_HINTS,
    SOCIAL_PLATFORM_HOSTS,
)

logger = logging.getLogger(__name__)

MAX_REDIRECTS_DEFAULT = 10
FETCH_RETRIES_DEFAULT = 3
FETCH_TIMEOUT_DEFAULT = 15.0

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_FETCH_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


class WebsiteStatusCode(str, Enum):
    ACTIVE = "ACTIVE"
    HTTP_404 = "404"
    DOMAIN_FOR_SALE = "DOMAIN_FOR_SALE"
    PARKED = "PARKED"
    BLANK_WIX = "BLANK_WIX"
    BLANK_SQUARESPACE = "BLANK_SQUARESPACE"
    NO_WEBSITE = "NO_WEBSITE"
    DEAD_DOMAIN = "DEAD_DOMAIN"
    UNREACHABLE = "UNREACHABLE"
    SOCIAL_REDIRECT = "SOCIAL_REDIRECT"
    DIRECTORY_REDIRECT = "DIRECTORY_REDIRECT"
    BUSINESS_WEBSITE_REDIRECT = "BUSINESS_WEBSITE_REDIRECT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


QUALIFIED_STATUSES: frozenset[WebsiteStatusCode] = frozenset(
    {
        WebsiteStatusCode.HTTP_404,
        WebsiteStatusCode.DOMAIN_FOR_SALE,
        WebsiteStatusCode.BLANK_WIX,
        WebsiteStatusCode.BLANK_SQUARESPACE,
        WebsiteStatusCode.NO_WEBSITE,
        WebsiteStatusCode.DEAD_DOMAIN,
        WebsiteStatusCode.UNREACHABLE,
        WebsiteStatusCode.SOCIAL_REDIRECT,
        WebsiteStatusCode.DIRECTORY_REDIRECT,
        WebsiteStatusCode.MANUAL_REVIEW,
    }
)

NOT_QUALIFIED_STATUSES: frozenset[WebsiteStatusCode] = frozenset(
    {
        WebsiteStatusCode.ACTIVE,
        WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT,
        WebsiteStatusCode.PARKED,
    }
)

REMOVE_LEAD_STATUSES: frozenset[WebsiteStatusCode] = NOT_QUALIFIED_STATUSES


@dataclass(frozen=True)
class WebsiteStatusResult:
    status: WebsiteStatusCode
    reason: str
    qualified: bool
    original_url: str = ""
    normalized_url: str = ""
    final_url: str = ""
    redirect_chain: tuple[str, ...] = ()
    http_status_code: int | None = None
    confidence: float = 1.0
    checked_at: str = ""
    error: str = ""
    detected_platform: str = ""

    def as_row_fields(self) -> dict[str, str]:
        from app.sheets.columns import (
            COL_CONFIDENCE,
            COL_FINAL_URL,
            COL_HTTP_STATUS_CODE,
            COL_ORIGINAL_WEBSITE_URL,
            COL_REDIRECT_CHAIN,
            COL_WEBSITE_CHECKED_AT,
            COL_WEBSITE_STATUS,
            COL_WEBSITE_STATUS_REASON,
        )

        return {
            COL_WEBSITE_STATUS: self.status.value,
            COL_WEBSITE_STATUS_REASON: self.reason,
            COL_HTTP_STATUS_CODE: str(self.http_status_code or ""),
            COL_ORIGINAL_WEBSITE_URL: self.original_url,
            COL_FINAL_URL: self.final_url,
            COL_REDIRECT_CHAIN: " → ".join(self.redirect_chain),
            COL_CONFIDENCE: f"{self.confidence:.2f}",
            COL_WEBSITE_CHECKED_AT: self.checked_at,
        }


_DOMAIN_FOR_SALE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"domain\s+for\s+sale",
        r"this\s+domain\s+is\s+for\s+sale",
        r"buy\s+this\s+domain",
        r"purchase\s+this\s+domain",
        r"make\s+an\s+offer",
        r"domain\s+name\s+available",
        r"premium\s+domain\s+is\s+available",
        r"acquire\s+this\s+domain",
    )
)

_PARKED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"this\s+domain\s+is\s+parked",
        r"domain\s+is\s+parked",
        r"parked\s+free",
        r"coming\s+soon",
        r"under\s+construction",
        r"site\s+not\s+published",
        r"future\s+home\s+of\s+something",
    )
)

_SOFT_404_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"page\s+not\s+found",
        r"404\s*[-–—]\s*page\s+not\s+found",
        r"this\s+page\s+doesn'?t\s+exist",
        r"page\s+you\s+requested\s+could\s+not\s+be\s+found",
        r"we\s+can'?t\s+find\s+that\s+page",
        r"sorry,\s+this\s+page\s+is\s+unavailable",
        r"error\s+404",
    )
)

_CAPTCHA_PATTERNS: tuple[str, ...] = (
    "verify you are human",
    "cf-challenge",
    "challenge-platform",
    "hcaptcha",
    "recaptcha",
)

_CAPTCHA_WEAK_PATTERNS: tuple[str, ...] = (
    "captcha",
    "cloudflare",
    "attention required",
    "security check",
)


def _normalize_host(host: str) -> str:
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches_set(host: str, hosts: frozenset[str]) -> bool:
    host = _normalize_host(host)
    if not host:
        return False
    for suffix in hosts:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _is_google_maps_path(host: str, path: str) -> bool:
    host = _normalize_host(host)
    path_lower = (path or "").lower()
    if "google." in host and "/maps" in path_lower:
        return True
    if host == "maps.app.goo.gl" or host.endswith(".goo.gl"):
        return True
    return False


def _is_directory_host(host: str, path: str = "") -> bool:
    if _is_google_maps_path(host, path):
        return True
    return _host_matches_set(host, DIRECTORY_PLATFORM_HOSTS)


def _is_social_host(host: str) -> bool:
    return _host_matches_set(host, SOCIAL_PLATFORM_HOSTS)


def _parse_url(raw: str) -> str | None:
    text = (raw or "").strip().strip("'\"")
    if not text:
        return None
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    try:
        parsed = urlparse(text)
        if not parsed.netloc:
            return None
        return text
    except Exception:
        return None


def _normalize_url(raw: str) -> tuple[str, str]:
    """Return (normalized_url, fetch_url)."""
    parsed = urlparse(_parse_url(raw) or "")
    host = _normalize_host(parsed.netloc)
    scheme = parsed.scheme or "https"
    path = parsed.path or "/"
    normalized = f"{scheme}://{host}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized, normalized


def _registrable_host(host: str) -> str:
    host = _normalize_host(host)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "org", "ac", "gov"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _hosts_differ_for_redirect(original_host: str, final_host: str) -> bool:
    orig = _registrable_host(original_host)
    final = _registrable_host(final_host)
    if not orig or not final:
        return False
    return orig != final


def _dns_resolves(host: str, timeout: float = 5.0) -> bool:
    host = _normalize_host(host)
    if not host:
        return False
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except socket.gaierror as exc:
        if exc.errno in {8, -2, -3}:  # NXDOMAIN / not found
            return False
        return False
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(None)


def _looks_domain_for_sale(html: str, text: str) -> bool:
    combined = f"{html} {text}"
    for pattern in _DOMAIN_FOR_SALE_PATTERNS:
        if pattern.search(combined):
            return True
    return False


def _looks_parked(html: str, text: str) -> bool:
    if _looks_domain_for_sale(html, text):
        return False
    combined = f"{html} {text}".lower()
    for pattern in _PARKED_PATTERNS:
        if pattern.search(combined):
            return True
    if "sedoparking" in combined or "parked free" in combined:
        return True
    return False


def _looks_soft_404(text: str, title: str) -> bool:
    combined = f"{title} {text}"
    for pattern in _SOFT_404_PATTERNS:
        if pattern.search(combined):
            return True
    title_lower = title.lower()
    if title_lower.startswith("404") or "404 not found" in title_lower:
        return True
    return False


def _is_wix_site(html: str) -> bool:
    lower = html.lower()
    return "wix.com" in lower or "wixstatic.com" in lower or "_wix" in lower


def _is_squarespace_site(html: str) -> bool:
    lower = html.lower()
    return "squarespace.com" in lower or "static.squarespace" in lower


def _meaningful_business_content(text: str, title: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 120:
        return False
    lower = f"{title} {stripped}".lower()
    markers = (
        "contact",
        "about",
        "service",
        "phone",
        "email",
        "welcome",
        "our ",
        "book",
        "opening",
    )
    hits = sum(1 for m in markers if m in lower)
    return hits >= 2


def _looks_blank_wix(html: str, text: str, title: str) -> bool:
    if not _is_wix_site(html):
        return False
    if _meaningful_business_content(text, title):
        return False
    lower = f"{html} {text}".lower()
    if "coming soon" in lower or "under construction" in lower:
        return True
    if len(text.strip()) < 150:
        return True
    if "wix.com" in lower and len(text.strip()) < 300:
        return True
    return False


def _looks_blank_squarespace(html: str, text: str, title: str) -> bool:
    if not _is_squarespace_site(html):
        return False
    if _meaningful_business_content(text, title):
        return False
    lower = f"{html} {text}".lower()
    if "coming soon" in lower or "under construction" in lower:
        return True
    if "this site is coming soon" in lower or "collection-type-pre-launch" in lower:
        return True
    if len(text.strip()) < 150:
        return True
    return False


def _looks_active_standalone(
    html: str,
    text: str,
    title: str,
    status_code: int,
) -> bool:
    if status_code >= 400:
        return False
    if _looks_domain_for_sale(html, text) or _looks_parked(html, text):
        return False
    if _looks_soft_404(text, title):
        return False
    if _looks_blank_wix(html, text, title) or _looks_blank_squarespace(html, text, title):
        return False
    stripped = text.strip()
    if len(stripped) < 80:
        return False
    return True


def _looks_manual_review(html: str, text: str) -> bool:
    """True only for thin challenge/interstitial pages — not full sites with bot widgets in HTML."""
    stripped = text.strip()
    if len(stripped) >= 1500:
        return False
    if len(stripped) >= 600 and _meaningful_business_content(stripped, extract_title(html)):
        return False

    combined = f"{html} {text}".lower()
    if any(p in combined for p in _CAPTCHA_PATTERNS):
        return True
    if len(stripped) < 300 and any(p in combined for p in _CAPTCHA_WEAK_PATTERNS):
        return True
    return False


def _is_bot_blocked_live_site(
    status_code: int,
    html: str,
    text: str,
) -> bool:
    """HTTP 401/403 with a real response body — site exists and blocks automated fetch."""
    if status_code not in (401, 403):
        return False
    if _looks_parked(html, text) or _looks_domain_for_sale(html, text):
        return False
    combined = f"{html} {text}".lower()
    if any(
        phrase in combined
        for phrase in (
            "account has expired",
            "website expired",
            "site not published",
            "hosting package has expired",
        )
    ):
        return False
    if _looks_soft_404(text, extract_title(html)):
        return False
    if len(html) > 5000 or len(text.strip()) > 200:
        return True
    return False


def _make_result(
    status: WebsiteStatusCode,
    reason: str,
    *,
    confidence: float = 1.0,
    original_url: str = "",
    normalized_url: str = "",
    final_url: str = "",
    redirect_chain: tuple[str, ...] = (),
    http_status_code: int | None = None,
    error: str = "",
    detected_platform: str = "",
) -> WebsiteStatusResult:
    qualified = status in QUALIFIED_STATUSES
    return WebsiteStatusResult(
        status=status,
        reason=reason,
        qualified=qualified,
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        redirect_chain=redirect_chain,
        http_status_code=http_status_code,
        confidence=confidence,
        checked_at=datetime.now().isoformat(timespec="seconds"),
        error=error,
        detected_platform=detected_platform,
    )


@dataclass
class _FetchResult:
    ok: bool
    status_code: int | None = None
    html: str = ""
    final_url: str = ""
    redirect_chain: tuple[str, ...] = ()
    error: str = ""
    dns_dead: bool = False
    ssl_error: bool = False


def _fetch_url(
    url: str,
    *,
    timeout: float,
    max_redirects: int,
    retries: int,
) -> _FetchResult:
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                max_redirects=max_redirects,
                headers=_FETCH_HEADERS,
            ) as client:
                response = client.get(url)
                chain: list[str] = [url]
                for hop in response.history:
                    chain.append(str(hop.url))
                chain.append(str(response.url))
                # Deduplicate consecutive
                deduped: list[str] = []
                for hop in chain:
                    if not deduped or deduped[-1] != hop:
                        deduped.append(hop)
                return _FetchResult(
                    ok=True,
                    status_code=response.status_code,
                    html=response.text[:200_000] if response.text else "",
                    final_url=str(response.url),
                    redirect_chain=tuple(deduped),
                )
        except httpx.TooManyRedirects:
            return _FetchResult(
                ok=False,
                error="redirect limit exceeded",
                redirect_chain=(url,),
            )
        except httpx.ConnectError as exc:
            last_error = str(exc)
            err_lower = last_error.lower()
            if "nxdomain" in err_lower or "name or service not known" in err_lower:
                return _FetchResult(ok=False, error=last_error, dns_dead=True)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if "ssl" in last_error.lower() or "certificate" in last_error.lower():
                return _FetchResult(ok=False, error=last_error, ssl_error=True)
        except OSError as exc:
            last_error = str(exc)
        if attempt + 1 < retries:
            continue
    return _FetchResult(ok=False, error=last_error or "fetch failed")


def _classify_destination(
    html: str,
    text: str,
    title: str,
    status_code: int,
    final_url: str,
) -> WebsiteStatusCode:
    """Classify redirect destination without re-fetching."""
    if status_code == 404 or _looks_soft_404(text, title):
        return WebsiteStatusCode.HTTP_404
    if _looks_domain_for_sale(html, text):
        return WebsiteStatusCode.DOMAIN_FOR_SALE
    if _looks_parked(html, text):
        return WebsiteStatusCode.PARKED
    if _looks_blank_wix(html, text, title):
        return WebsiteStatusCode.BLANK_WIX
    if _looks_blank_squarespace(html, text, title):
        return WebsiteStatusCode.BLANK_SQUARESPACE
    if status_code and status_code >= 500:
        return WebsiteStatusCode.UNREACHABLE
    if _looks_active_standalone(html, text, title, status_code or 0):
        return WebsiteStatusCode.ACTIVE
    return WebsiteStatusCode.MANUAL_REVIEW


def classify_website_link(
    raw_url: str,
    *,
    timeout: float = FETCH_TIMEOUT_DEFAULT,
    max_redirects: int = MAX_REDIRECTS_DEFAULT,
    retries: int = FETCH_RETRIES_DEFAULT,
) -> WebsiteStatusResult:
    """
    Classify website status from Website Link value.

    Deterministic checks first; does not use LLM for primary classification.
    """
    original = (raw_url or "").strip()
    if not original:
        return _make_result(
            WebsiteStatusCode.NO_WEBSITE,
            "No website URL supplied in Website Link.",
            confidence=1.0,
            original_url="",
        )

    parsed_https = _parse_url(original)
    if not parsed_https:
        return _make_result(
            WebsiteStatusCode.MANUAL_REVIEW,
            "Website Link value could not be parsed as a URL.",
            confidence=0.5,
            original_url=original,
        )

    normalized, fetch_url = _normalize_url(original)
    original_host = _normalize_host(urlparse(normalized).netloc)

    if not _dns_resolves(original_host, timeout=min(timeout, 5.0)):
        return _make_result(
            WebsiteStatusCode.DEAD_DOMAIN,
            "Domain does not resolve (DNS failure / NXDOMAIN).",
            original_url=original,
            normalized_url=normalized,
            confidence=0.9,
        )

    fetch = _fetch_url(
        fetch_url,
        timeout=timeout,
        max_redirects=max_redirects,
        retries=retries,
    )

    if not fetch.ok:
        if fetch.dns_dead:
            return _make_result(
                WebsiteStatusCode.DEAD_DOMAIN,
                "Domain does not resolve.",
                original_url=original,
                normalized_url=normalized,
                error=fetch.error,
                confidence=0.9,
            )
        if len(fetch.redirect_chain) > max_redirects:
            return _make_result(
                WebsiteStatusCode.MANUAL_REVIEW,
                "Redirect chain exceeded maximum length.",
                original_url=original,
                normalized_url=normalized,
                redirect_chain=fetch.redirect_chain,
                confidence=0.5,
                error=fetch.error,
            )
        # Try HTTP fallback if HTTPS failed
        if fetch_url.startswith("https://"):
            http_url = fetch_url.replace("https://", "http://", 1)
            fetch = _fetch_url(
                http_url,
                timeout=timeout,
                max_redirects=max_redirects,
                retries=retries,
            )
        if not fetch.ok:
            return _make_result(
                WebsiteStatusCode.UNREACHABLE,
                f"Website could not be reached after {retries} attempt(s).",
                original_url=original,
                normalized_url=normalized,
                redirect_chain=fetch.redirect_chain,
                error=fetch.error,
                confidence=0.85,
            )

    final_parsed = urlparse(fetch.final_url or fetch_url)
    final_host = _normalize_host(final_parsed.netloc)
    final_path = final_parsed.path or ""
    html = fetch.html
    text = html_to_text(html)
    title = extract_title(html)
    status_code = fetch.status_code or 0

    if len(fetch.redirect_chain) > max_redirects + 1:
        return _make_result(
            WebsiteStatusCode.MANUAL_REVIEW,
            "Redirect chain exceeded maximum length.",
            original_url=original,
            normalized_url=normalized,
            final_url=fetch.final_url,
            redirect_chain=fetch.redirect_chain,
            http_status_code=status_code,
            confidence=0.5,
        )

    base_kwargs: dict[str, Any] = {
        "original_url": original,
        "normalized_url": normalized,
        "final_url": fetch.final_url,
        "redirect_chain": fetch.redirect_chain,
        "http_status_code": status_code,
    }

    if _is_social_host(final_host):
        platform = final_host
        return _make_result(
            WebsiteStatusCode.SOCIAL_REDIRECT,
            f"Business domain redirects to social media ({platform}) rather than a standalone website.",
            detected_platform=platform,
            confidence=0.95,
            **base_kwargs,
        )

    if _is_directory_host(final_host, final_path):
        return _make_result(
            WebsiteStatusCode.DIRECTORY_REDIRECT,
            f"Business domain redirects to a third-party business directory ({final_host}).",
            detected_platform=final_host,
            confidence=0.95,
            **base_kwargs,
        )

    if _hosts_differ_for_redirect(original_host, final_host):
        dest_status = _classify_destination(html, text, title, status_code, fetch.final_url)
        if dest_status == WebsiteStatusCode.ACTIVE:
            return _make_result(
                WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT,
                f"Domain redirects to another functioning business website ({final_host}).",
                confidence=0.9,
                **base_kwargs,
            )
        if dest_status == WebsiteStatusCode.MANUAL_REVIEW:
            return _make_result(
                WebsiteStatusCode.MANUAL_REVIEW,
                f"Domain redirects to {final_host} but destination type is unclear.",
                confidence=0.55,
                **base_kwargs,
            )
        # Destination is qualified type (social/directory already handled) — treat as redirect to non-business
        return _make_result(
            WebsiteStatusCode.MANUAL_REVIEW,
            f"Domain redirects to {final_host} ({dest_status.value}).",
            confidence=0.6,
            **base_kwargs,
        )

    if status_code == 404:
        return _make_result(
            WebsiteStatusCode.HTTP_404,
            "Website returned HTTP 404.",
            confidence=0.95,
            **base_kwargs,
        )

    if _looks_soft_404(text, title):
        return _make_result(
            WebsiteStatusCode.HTTP_404,
            "Page content indicates the site/page does not exist.",
            confidence=0.9,
            **base_kwargs,
        )

    if _looks_domain_for_sale(html, text):
        return _make_result(
            WebsiteStatusCode.DOMAIN_FOR_SALE,
            "Page indicates the domain is for sale or available to purchase.",
            confidence=0.9,
            **base_kwargs,
        )

    if _looks_blank_wix(html, text, title):
        return _make_result(
            WebsiteStatusCode.BLANK_WIX,
            "Wix-hosted page loads but contains no meaningful business content.",
            detected_platform="wix",
            confidence=0.88,
            **base_kwargs,
        )

    if _looks_blank_squarespace(html, text, title):
        return _make_result(
            WebsiteStatusCode.BLANK_SQUARESPACE,
            "Squarespace page loads but contains no meaningful business content.",
            detected_platform="squarespace",
            confidence=0.88,
            **base_kwargs,
        )

    if _looks_parked(html, text):
        return _make_result(
            WebsiteStatusCode.PARKED,
            "Domain appears parked or showing a generic placeholder page.",
            confidence=0.85,
            **base_kwargs,
        )

    if status_code >= 500:
        return _make_result(
            WebsiteStatusCode.UNREACHABLE,
            f"Server error HTTP {status_code}.",
            confidence=0.85,
            **base_kwargs,
        )

    if _looks_active_standalone(html, text, title, status_code):
        return _make_result(
            WebsiteStatusCode.ACTIVE,
            "Functioning standalone business website detected.",
            confidence=0.9,
            **base_kwargs,
        )

    if _is_bot_blocked_live_site(status_code, html, text):
        return _make_result(
            WebsiteStatusCode.ACTIVE,
            f"Active website (HTTP {status_code}, blocks bots but site is live).",
            confidence=0.88,
            **base_kwargs,
        )

    if _looks_manual_review(html, text):
        return _make_result(
            WebsiteStatusCode.MANUAL_REVIEW,
            "Anti-bot or CAPTCHA page detected — manual review required.",
            confidence=0.5,
            **base_kwargs,
        )

    return _make_result(
        WebsiteStatusCode.MANUAL_REVIEW,
        "Website is accessible but automated classification cannot determine status confidently.",
        confidence=0.55,
        **base_kwargs,
    )


def status_counts(results: list[WebsiteStatusResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = result.status.value
        counts[key] = counts.get(key, 0) + 1
    return counts
