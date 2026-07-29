"""Parse uploaded lead lists (Facebook Link + Business Name)."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from app.scrapers.lead_mapping import normalize_facebook_url

_URL_LIKE = re.compile(r"https?://|facebook\.com|www\.", re.I)

_LINK_HEADERS = frozenset(
    {
        "facebook link",
        "facebook url",
        "fb link",
        "link",
        "url",
        "page link",
        "profile",
        "facebook",
    }
)
_NAME_HEADERS = frozenset(
    {
        "business name",
        "name",
        "page name",
        "company",
        "company name",
        "business",
        "organisation",
        "organization",
        "display name",
        "title",
        "account name",
    }
)
_LINK_HEADERS_EXTRA = frozenset(
    {
        "facebook page",
        "page url",
        "profile url",
        "profile link",
        "fb url",
    }
)


class LeadImportError(Exception):
    """Invalid upload format or no usable rows."""


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _looks_like_url(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    if _URL_LIKE.search(text):
        return True
    return "facebook.com" in text.lower()


_JOINED_METADATA_RE = re.compile(r",\s*Joined\s+", re.I)
_FOLLOWER_METADATA_RE = re.compile(
    r"\d[\d,]*\s+people follow this|person follows this",
    re.I,
)


def _is_facebook_metadata(text: str) -> bool:
    """Columns/cells that are not the business name in Facebook exports."""
    t = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if not t:
        return True
    if t in {"follow", "·", "-", "—"}:
        return True
    if t.startswith("joined ") and " ago" in t:
        return True
    if _FOLLOWER_METADATA_RE.search(t):
        return True
    if re.match(r"^\d[\d,]*\s+followers?$", t):
        return True
    return False


def _clean_business_name(raw: str) -> str:
    """Strip Facebook page/group UI text accidentally included in name fields."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""

    joined = _JOINED_METADATA_RE.search(text)
    if joined:
        text = text[:joined.start()].strip().rstrip(",")

    if "·" in text:
        head = text.split("·")[0].strip().rstrip(",")
        if head:
            text = head

    text = re.sub(r",?\s*Follow\s*$", "", text, flags=re.I).strip()
    return text


def _name_from_parts(parts: list[str], link_col: int) -> str:
    """First non-link name column(s) until Facebook metadata columns."""
    chunks: list[str] = []
    for idx, part in enumerate(parts):
        if idx == link_col:
            continue
        text = str(part or "").strip()
        if not text:
            continue
        if _is_facebook_metadata(text):
            break
        chunks.append(text)
    if not chunks:
        return ""
    if len(chunks) == 1:
        return _clean_business_name(chunks[0])
    return _clean_business_name(", ".join(chunks))


def _detect_url_and_name_columns(cells: list[str]) -> tuple[int, int | None]:
    """Find link column (URL) and name column (any other populated column)."""
    link_col = 0
    for idx, cell in enumerate(cells):
        if _looks_like_url(str(cell or "")):
            link_col = idx
            break
    name_col: int | None = None
    for idx, cell in enumerate(cells):
        if idx == link_col:
            continue
        text = str(cell or "").strip()
        if text and not _is_facebook_metadata(text):
            name_col = idx
            break
    return link_col, name_col


def _split_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if "\t" in stripped:
        parts = stripped.split("\t")
    elif "," in stripped:
        try:
            parts = next(csv.reader([stripped]))
        except csv.Error:
            parts = stripped.split(",", 1)
    else:
        parts = re.split(r"\s{2,}|\|", stripped)

    parts = [str(p or "").strip() for p in parts]
    if not parts:
        return None

    if len(parts) == 1:
        url = parts[0]
        return (url, "") if _looks_like_url(url) else None

    link_col, _ = _detect_url_and_name_columns(parts)
    url = parts[link_col] if link_col < len(parts) else parts[0]
    name = _name_from_parts(parts, link_col)
    if not _looks_like_url(url):
        return None
    return (url, name)


def _parse_csv(text: str) -> list[tuple[str, str]]:
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []

    header_row = [_normalize_header(cell) for cell in rows[0]]
    link_col: int | None = None
    name_col: int | None = None
    matched_name_header = False
    all_link_headers = _LINK_HEADERS | _LINK_HEADERS_EXTRA
    for index, header in enumerate(header_row):
        if header in all_link_headers:
            link_col = index
        if header in _NAME_HEADERS:
            name_col = index
            matched_name_header = True

    has_header = link_col is not None or name_col is not None
    start = 1 if has_header else 0

    if link_col is None:
        link_col, name_col = _detect_url_and_name_columns(
            [str(c or "") for c in rows[0]]
        )
    elif name_col is None and len(header_row) > 1:
        for index in range(len(header_row)):
            if index != link_col:
                name_col = index
                break

    leads: list[tuple[str, str]] = []
    for row in rows[start:]:
        if not row:
            continue
        if link_col < len(row):
            url = str(row[link_col] or "").strip()
        else:
            url = str(row[0] or "").strip()
        row_parts = [str(c or "") for c in row]
        if matched_name_header and name_col is not None and name_col < len(row):
            name = _clean_business_name(str(row[name_col] or ""))
        else:
            name = _name_from_parts(row_parts, link_col)
        if url and _looks_like_url(url):
            leads.append((url, name))
    return leads


def parse_lead_list(text: str) -> list[dict[str, str]]:
    """Return normalized leads: facebook_link, business_name."""
    raw = (text or "").strip()
    if not raw:
        raise LeadImportError("Paste or upload a lead list.")

    candidates: list[tuple[str, str]] = []
    if "," in raw[:500] or "\t" in raw[:500]:
        try:
            candidates = _parse_csv(raw)
        except csv.Error:
            candidates = []

    if not candidates:
        for line in raw.splitlines():
            parsed = _split_line(line)
            if parsed:
                candidates.append(parsed)

    if not candidates:
        raise LeadImportError(
            "No leads found. Use CSV/TSV with link + name columns, or lines like: "
            "https://facebook.com/..., Business Name"
        )

    leads: list[dict[str, str]] = []
    seen: set[str] = set()
    for url, name in candidates:
        link = normalize_facebook_url(url)
        if not link or "facebook.com" not in link.lower():
            continue
        key = link.lower()
        if key in seen:
            continue
        seen.add(key)
        leads.append(
            {
                "facebook_link": link,
                "business_name": _clean_business_name(name),
            }
        )

    if not leads:
        raise LeadImportError("No valid Facebook links found in upload.")
    return leads
