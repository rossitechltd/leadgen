"""Domain-aware email extraction from profile scrape text."""

from __future__ import annotations

import re

from app.qualify.website import (
    _DOMAIN_IN_TEXT,
    _domain_tokens_from_text,
    is_trackable_website,
)

EMAIL_IN_TEXT = re.compile(
    r"\b[a-z0-9._%+-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,}\b",
    re.IGNORECASE,
)

_PREFERRED_LOCALS: tuple[str, ...] = (
    "info",
    "contact",
    "enquiries",
    "hello",
    "sales",
    "office",
)


def _normalize_host(host: str) -> str:
    token = (host or "").lower().strip()
    if token.startswith("www."):
        token = token[4:]
    return token


def _email_domain(email: str) -> str:
    parts = email.rsplit("@", 1)
    if len(parts) != 2:
        return ""
    return _normalize_host(parts[1])


def collect_website_domains(scrape_text: str, website_link: str = "") -> set[str]:
    """Business website domains from website_link and scrape text."""
    domains: set[str] = set()

    for host in _domain_tokens_from_text(website_link):
        normalized = _normalize_host(host)
        if normalized and is_trackable_website(f"https://{normalized}"):
            domains.add(normalized)

    if scrape_text:
        for match in _DOMAIN_IN_TEXT.finditer(scrape_text):
            normalized = _normalize_host(match.group(1))
            if normalized and is_trackable_website(f"https://{normalized}"):
                domains.add(normalized)

    return domains


def find_emails_for_domains(text: str, domains: set[str]) -> list[str]:
    """Emails in text whose domain matches one of the collected website domains."""
    if not text or not domains:
        return []

    seen: set[str] = set()
    matches: list[str] = []
    for match in EMAIL_IN_TEXT.finditer(text):
        email = match.group(0)
        key = email.lower()
        if key in seen:
            continue
        if _email_domain(email) in domains:
            seen.add(key)
            matches.append(email)
    return matches


def pick_best_email(emails: list[str]) -> str:
    if not emails:
        return ""
    if len(emails) == 1:
        return emails[0]

    def preference_rank(email: str) -> int:
        local = email.rsplit("@", 1)[0].lower().split("+")[0]
        try:
            return _PREFERRED_LOCALS.index(local)
        except ValueError:
            return len(_PREFERRED_LOCALS)

    return min(emails, key=preference_rank)


def resolve_profile_email(
    llm_email: str,
    *,
    scrape_text: str,
    website_link: str = "",
) -> str:
    """
    Prefer scrape emails that match known business website domain(s).

    Falls back to LLM email only when its domain matches a collected domain
    and no domain-matched email appears in scrape text.
    """
    domains = collect_website_domains(scrape_text, website_link)
    scrape_matches = find_emails_for_domains(scrape_text, domains)

    if scrape_matches:
        return pick_best_email(scrape_matches)

    llm = (llm_email or "").strip()
    if llm and _email_domain(llm) in domains:
        return llm

    return ""
