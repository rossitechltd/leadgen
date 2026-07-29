"""Regression tests for unified scrape paste ownership."""

from __future__ import annotations

from app.scrape_queue.ownership import (
    evaluate_paste_for_intended,
    ownership_action_label,
    paste_belongs_to_intended,
    PasteOwnershipStatus,
    resolve_paste_owner,
)
from app.scrape_queue.state import PHASE_AWAITING_CLEAR, PHASE_AWAITING_PASTE, PHASE_READY

MIN_LENGTH = 50

LEAD_ROWS = [
    (2, "https://www.facebook.com/profile.php?id=61552393360978", "James Mutuku", "scraping", 0),
    (3, "https://www.facebook.com/TLCdrainage", "TLC Drainage", "scraped", 500),
    (4, "https://www.facebook.com/aaschoff", "Adam Aschoff", "scraping", 0),
    (8, "https://www.facebook.com/tom.ryan.96", "Tom Ryan", "scraped", 500),
    (14, "https://www.facebook.com/profile.php?id=61552941270075", "Erin Short", "scraped", 500),
    (6, "https://www.facebook.com/profile.php?id=61592310532825", "Child Contact Plymouth", "scraped", 600),
]

JAMES_SPARSE = """
𝟯.𝟰𝗄 𝐟𝐨𝐥𝐥𝐨𝐰𝐞𝐫𝐬 • 𝟰𝟳𝟲 𝐟𝐨𝐥𝐥𝐨𝐰𝐢𝐧𝐠
Photos
Privacy  · Terms  · Advertising  · Ad choices   · Cookies  ·
Posts
No posts available
"""

TLC_PASTE = """
168 followers • 13 following
Drainage & Property Maintenance Services
TLC Drainage
Photos
Privacy  · Terms  · Advertising  · Ad choices   · Cookies  ·
Posts
Facebook
TLC Drainage updated their cover photo.
Merry Christmas to all our customers past, present and future !
"""

CHILD_CONTACT_PASTE = """
53 followers • 6 following
Fostering Safe and Supportive Family Meetings
Child Contact Plymouth exists so that no child misses out on a loving relationship
childcontactplymouth.org.uk
contact@childcontactplymouth.org.uk
Child Contact Plymouth
Photos
Privacy  · Terms  · Advertising  · Ad choices   · Cookies  ·
Posts
"""


def _eval(
    paste: str,
    row: int,
    name: str,
    *,
    phase: str = PHASE_AWAITING_PASTE,
    baseline: str = "",
    consumed: str = "",
):
    return evaluate_paste_for_intended(
        paste,
        row,
        name,
        LEAD_ROWS,
        min_length=MIN_LENGTH,
        phase=phase,
        baseline_b_hash=baseline,
        consumed_paste_hash=consumed,
    )


def test_james_sparse_profile():
    result = _eval(JAMES_SPARSE, 2, "James Mutuku")
    assert result.status == PasteOwnershipStatus.SPARSE_OK
    assert paste_belongs_to_intended(result, 2)
    assert ownership_action_label(result) == "SPARSE_OK"


def test_tlc_correct_match():
    result = _eval(TLC_PASTE, 3, "TLC Drainage")
    assert result.status == PasteOwnershipStatus.MATCH
    assert paste_belongs_to_intended(result, 3)


def test_tom_stale_tlc_paste():
    result = _eval(TLC_PASTE, 8, "Tom Ryan")
    assert result.status == PasteOwnershipStatus.WRONG_LEAD
    assert not paste_belongs_to_intended(result, 8)
    assert "TLC" in ownership_action_label(result)


def test_erin_stale_child_contact_paste():
    result = _eval(CHILD_CONTACT_PASTE, 14, "Erin Short")
    assert result.status == PasteOwnershipStatus.WRONG_LEAD
    assert not paste_belongs_to_intended(result, 14)


def test_adam_empty_not_ready():
    result = _eval("", 4, "Adam Aschoff")
    assert result.status == PasteOwnershipStatus.NOT_READY


def test_carried_paste_same_baseline_blocked():
    from app.scrape_queue.ownership import hash_paste_text

    baseline = hash_paste_text(TLC_PASTE)
    result = _eval(
        TLC_PASTE,
        8,
        "Tom Ryan",
        phase=PHASE_AWAITING_PASTE,
        baseline=baseline,
    )
    assert result.status == PasteOwnershipStatus.NOT_READY
    assert "carried-over" in result.reason


def test_resolve_paste_owner_tlc():
    result = resolve_paste_owner(TLC_PASTE, LEAD_ROWS, min_length=MIN_LENGTH)
    assert result.status == PasteOwnershipStatus.MATCH
    assert result.matched_row == 3


def test_awaiting_clear_blocks_paste():
    result = _eval(JAMES_SPARSE, 2, "James Mutuku", phase=PHASE_AWAITING_CLEAR)
    assert result.status == PasteOwnershipStatus.NOT_READY


def test_ready_phase_allows_sparse():
    result = _eval(JAMES_SPARSE, 2, "James Mutuku", phase=PHASE_READY)
    assert paste_belongs_to_intended(result, 2)


def test_link_matched_row_accepts_ambiguous_multi_name_paste():
    from app.scrape_queue.ownership import evaluate_paste_for_link_matched_row

    rows = [
        (2, "https://www.facebook.com/a", "South Devon Decorators", "", 0),
        (3, "https://www.facebook.com/b", "Mr. Down's Tutoring Services", "", 0),
        (4, "https://www.facebook.com/c", "SoundWave Radio Plymouth", "", 0),
        (21, "https://www.facebook.com/d", "The Spire Music Academy", "", 0),
        (22, "https://www.facebook.com/e", "Be Well Be Whole", "", 0),
    ]
    paste = (
        "Mr. Down's Tutoring Services\n"
        "The Spire Music Academy\n"
        "Be Well Be Whole\n"
        "SoundWave Radio Plymouth\n"
        + "Facebook\n" * 30
        + "47 followers • 0 following\n"
        "Hi, I'm James Down, a qualified primary school teacher\n"
        "mrdownstutoring.co.uk\n"
    )
    result = evaluate_paste_for_link_matched_row(
        paste,
        3,
        "Mr. Down's Tutoring Services",
        rows,
        min_length=MIN_LENGTH,
    )
    assert result.status == PasteOwnershipStatus.MATCH
    assert paste_belongs_to_intended(result, 3)
