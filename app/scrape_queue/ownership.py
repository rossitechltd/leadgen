"""Unified paste ownership — single gate for handoff and save paths."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.scrape_queue.verify import (
    looks_like_sparse_facebook_scrape,
    verify_scrape_matches_business,
    verify_scrape_text,
)

LeadRow = tuple[int, str, str, str, int]  # row, link, name, activity, scrape_len

B_EMPTY_MAX_CHARS = 2


class PasteOwnershipStatus(str, Enum):
    MATCH = "match"
    SPARSE_OK = "sparse_ok"
    WRONG_LEAD = "wrong_lead"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class PasteOwnerResult:
    status: PasteOwnershipStatus
    matched_row: int | None = None
    matched_name: str = ""
    wrong_lead_row: int | None = None
    wrong_lead_name: str = ""
    reason: str = ""


def normalize_paste_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def hash_paste_text(text: str) -> str:
    normalized = normalize_paste_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def b_is_empty(scrape_text: str) -> bool:
    return len((scrape_text or "").strip()) <= B_EMPTY_MAX_CHARS


def strict_name_matches(
    scrape_text: str,
    business_name: str,
    *,
    min_length: int,
) -> bool:
    if not (business_name or "").strip():
        return True
    result = verify_scrape_text(
        scrape_text,
        min_length=min_length,
        business_name=business_name,
        lenient_name=False,
        allow_sparse_profile=False,
    )
    return result.ok


def find_strict_name_matches(
    scrape_text: str,
    lead_rows: Sequence[LeadRow],
    *,
    min_length: int,
) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    for row_index, _link, name, _activity, _scrape_len in lead_rows:
        if not (name or "").strip():
            continue
        if strict_name_matches(scrape_text, name, min_length=min_length):
            matches.append((row_index, name.strip()))
    return matches


def resolve_paste_owner(
    scrape_text: str,
    lead_rows: Sequence[LeadRow],
    *,
    min_length: int,
) -> PasteOwnerResult:
    """Identify which lead (if any) the paste text belongs to."""
    if b_is_empty(scrape_text):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="column B empty",
        )

    basic = verify_scrape_text(
        scrape_text,
        min_length=min_length,
        business_name="",
        allow_sparse_profile=False,
    )
    if not basic.ok:
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason=basic.reason,
        )

    strict_matches = find_strict_name_matches(
        scrape_text, lead_rows, min_length=min_length
    )
    if len(strict_matches) == 1:
        row_index, name = strict_matches[0]
        return PasteOwnerResult(
            status=PasteOwnershipStatus.MATCH,
            matched_row=row_index,
            matched_name=name,
            reason="strict name match",
        )
    if len(strict_matches) > 1:
        rows = ", ".join(f"{r} ({n})" for r, n in strict_matches)
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason=f"ambiguous paste matches multiple leads: {rows}",
        )

    if looks_like_sparse_facebook_scrape(scrape_text):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.SPARSE_OK,
            reason="sparse profile — no name in paste",
        )

    return PasteOwnerResult(
        status=PasteOwnershipStatus.NOT_READY,
        reason="paste not matched to any lead name",
    )


def evaluate_paste_for_link_matched_row(
    scrape_text: str,
    intended_row: int,
    intended_name: str,
    lead_rows: Sequence[LeadRow],
    *,
    min_length: int,
) -> PasteOwnerResult:
    """
    Ownership when scrapesheet column A already identifies the lead.

    FB pastes often mention multiple business names (suggested pages, posts).
    Trust the link match unless the paste clearly belongs to a different lead only.
    """
    if b_is_empty(scrape_text):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="awaiting MMM paste",
        )

    basic = verify_scrape_text(
        scrape_text,
        min_length=min_length,
        business_name="",
        allow_sparse_profile=False,
    )
    if not basic.ok:
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason=basic.reason,
        )

    strict_matches = find_strict_name_matches(
        scrape_text, lead_rows, min_length=min_length
    )
    intended_matches = [m for m in strict_matches if m[0] == intended_row]
    other_matches = [m for m in strict_matches if m[0] != intended_row]

    if other_matches and not intended_matches:
        row_index, name = other_matches[0]
        return PasteOwnerResult(
            status=PasteOwnershipStatus.WRONG_LEAD,
            wrong_lead_row=row_index,
            wrong_lead_name=name,
            reason=f"paste matches row {row_index} ({name})",
        )

    if intended_matches:
        _, name = intended_matches[0]
        return PasteOwnerResult(
            status=PasteOwnershipStatus.MATCH,
            matched_row=intended_row,
            matched_name=name,
            reason="strict name match (link row)",
        )

    if (intended_name or "").strip():
        name_check = verify_scrape_matches_business(
            scrape_text,
            intended_name,
            allow_sparse_profile=True,
        )
        if name_check.ok:
            return PasteOwnerResult(
                status=PasteOwnershipStatus.MATCH,
                matched_row=intended_row,
                matched_name=intended_name.strip(),
                reason="name match (link row)",
            )

    if looks_like_sparse_facebook_scrape(scrape_text):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.SPARSE_OK,
            matched_row=intended_row,
            matched_name=intended_name.strip(),
            reason="sparse profile (link row)",
        )

    return PasteOwnerResult(
        status=PasteOwnershipStatus.MATCH,
        matched_row=intended_row,
        matched_name=intended_name.strip(),
        reason="valid scrape for link-matched row",
    )


def evaluate_paste_for_intended(
    scrape_text: str,
    intended_row: int,
    intended_name: str,
    lead_rows: Sequence[LeadRow],
    *,
    min_length: int,
    phase: str,
    baseline_b_hash: str,
    consumed_paste_hash: str,
    trust_link: bool = False,
) -> PasteOwnerResult:
    """
    Decide whether column B paste can be saved for the lead in column A.

    Requires clear-then-paste cycle: baseline hash must not be re-saved without clear.
    """
    if b_is_empty(scrape_text):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="awaiting MMM paste",
        )

    data_hash = hash_paste_text(scrape_text)
    if consumed_paste_hash and data_hash == consumed_paste_hash:
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="paste already saved this cycle",
        )

    if phase == "awaiting_clear":
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="awaiting MMM to clear column B",
        )

    if (
        baseline_b_hash
        and data_hash == baseline_b_hash
        and phase == "awaiting_paste"
    ):
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason="carried-over paste — waiting for clear+paste cycle",
        )

    if trust_link:
        link_result = evaluate_paste_for_link_matched_row(
            scrape_text,
            intended_row,
            intended_name,
            lead_rows,
            min_length=min_length,
        )
        if link_result.status != PasteOwnershipStatus.NOT_READY:
            return link_result

    resolved = resolve_paste_owner(scrape_text, lead_rows, min_length=min_length)

    if resolved.status == PasteOwnershipStatus.MATCH:
        if resolved.matched_row == intended_row:
            return resolved
        return PasteOwnerResult(
            status=PasteOwnershipStatus.WRONG_LEAD,
            wrong_lead_row=resolved.matched_row,
            wrong_lead_name=resolved.matched_name,
            reason=f"paste matches row {resolved.matched_row} ({resolved.matched_name})",
        )

    if resolved.status == PasteOwnershipStatus.SPARSE_OK:
        # Sparse paste — only valid for intended row if no other lead strict-matches.
        for row_index, name in find_strict_name_matches(
            scrape_text, lead_rows, min_length=min_length
        ):
            if row_index != intended_row:
                return PasteOwnerResult(
                    status=PasteOwnershipStatus.WRONG_LEAD,
                    wrong_lead_row=row_index,
                    wrong_lead_name=name,
                    reason=f"sparse paste contains name for row {row_index} ({name})",
                )
        name_check = verify_scrape_matches_business(
            scrape_text,
            intended_name,
            allow_sparse_profile=True,
        )
        if name_check.ok:
            return PasteOwnerResult(
                status=PasteOwnershipStatus.SPARSE_OK,
                matched_row=intended_row,
                matched_name=intended_name,
                reason="sparse profile for intended lead",
            )
        return PasteOwnerResult(
            status=PasteOwnershipStatus.NOT_READY,
            reason=name_check.reason or "sparse paste not accepted",
        )

    if resolved.status == PasteOwnershipStatus.WRONG_LEAD:
        return resolved

    return resolved


def paste_belongs_to_intended(result: PasteOwnerResult, intended_row: int) -> bool:
    if result.status == PasteOwnershipStatus.MATCH:
        return result.matched_row == intended_row
    if result.status == PasteOwnershipStatus.SPARSE_OK:
        return result.matched_row == intended_row
    return False


def ownership_action_label(result: PasteOwnerResult) -> str:
    if result.status == PasteOwnershipStatus.MATCH:
        return "MATCH"
    if result.status == PasteOwnershipStatus.SPARSE_OK:
        return "SPARSE_OK"
    if result.status == PasteOwnershipStatus.WRONG_LEAD:
        who = result.wrong_lead_name or f"row {result.wrong_lead_row}"
        return f"WRONG_LEAD ({who})"
    return f"WAIT ({result.reason})"
