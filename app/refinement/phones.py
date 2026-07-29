"""UK phone normalisation for Profile Refinement."""

from __future__ import annotations

import re

_DIGIT = re.compile(r"\D")
_LETTERS = re.compile(r"[a-zA-Z]")


def normalize_uk_phone(raw: str | None) -> str:
    """
  Standardise to UK format without + or leading apostrophe.

  Examples:
    07739 881539   → 44 7739 881539
    +447123123456  → 44 7123 123456
    '44 7739 881539 → 44 7739 881539
    """
    if not raw:
        return ""
    text = str(raw).strip().lstrip("'").lstrip("+")
    if _LETTERS.search(text):
        return ""

    digits = _DIGIT.sub("", text)
    if not digits:
        return ""

    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("44"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    if len(national) < 10:
        return ""
    if len(national) > 10:
        national = national[-10:]

    return f"44 {national[:4]} {national[4:]}"


def format_uk_phone_display(normalized: str) -> str:
    """Display UK phone in local format for refined text, e.g. 07803 397724."""
    if not normalized:
        return ""
    digits = _DIGIT.sub("", normalized.lstrip("'").lstrip("+"))
    if digits.startswith("44"):
        national = digits[2:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    if len(national) < 10:
        return normalized.strip()
    if len(national) > 10:
        national = national[-10:]

    return f"0{national[:4]} {national[4:]}"
