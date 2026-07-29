"""Format structured profile fields as the refined column bullet list."""

from __future__ import annotations

NOT_FOUND = "notfound"

_FIELDS: tuple[tuple[str, str], ...] = (
    ("business_name", "Business name"),
    ("business_type", "Business type"),
    ("location", "Location"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("description", "Description of business/services"),
)


def _value_or_notfound(value: str) -> str:
    text = (value or "").strip()
    return text if text else NOT_FOUND


def format_refined_text(
    *,
    business_name: str = "",
    business_type: str = "",
    location: str = "",
    phone: str = "",
    email: str = "",
    description: str = "",
) -> str:
    """
    Always output all six bullets; missing values use "notfound".

    Example:
    • Business name: SOS spraying & Repairs
    • Business type: spraying & repairs
    ...
    """
    values = {
        "business_name": business_name,
        "business_type": business_type,
        "location": location,
        "phone": phone,
        "email": email,
        "description": description,
    }
    lines = [
        f"• {label}: {_value_or_notfound(values[key])}"
        for key, label in _FIELDS
    ]
    return "\n".join(lines)
