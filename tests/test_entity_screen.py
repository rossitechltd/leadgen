"""Tests for Phase 1 entity screen heuristics and tagging rules."""

from app.entity.constants import (
    LEAD_ACTIVITY_ENTITY_BUSINESS,
    LEAD_ACTIVITY_ENTITY_UNCERTAIN,
)
from app.entity.heuristics import heuristic_screen
from app.entity.screen import _ENTITY_TAGS


def test_people_url_is_person():
    result = heuristic_screen("John Smith", "https://www.facebook.com/people/john.smith")
    assert result.entity_type == "person"
    assert result.confidence >= 0.88
    assert "people" in result.reason.lower()


def test_profile_php_personal_name_is_person():
    result = heuristic_screen(
        "Jane Doe",
        "https://www.facebook.com/profile.php?id=100012345678901",
    )
    assert result.entity_type == "person"
    assert result.confidence >= 0.88


def test_business_keywords_in_name_is_business():
    result = heuristic_screen(
        "Sparkpro Carpet Cleaning",
        "https://www.facebook.com/sparkprocarpet",
    )
    assert result.entity_type == "business"
    assert result.confidence >= 0.85
    assert "business" in result.reason.lower()


def test_trade_name_with_ltd_is_business():
    result = heuristic_screen(
        "Pinnacle Builders Ltd",
        "https://www.facebook.com/pinnaclebuilders",
    )
    assert result.entity_type == "business"
    assert result.confidence >= 0.85


def test_ambiguous_name_no_strong_signal():
    result = heuristic_screen(
        "Sarah Mitchell",
        "https://www.facebook.com/sarahmitchellpage",
    )
    assert result.entity_type == ""
    assert result.confidence == 0.0


def test_personal_name_on_page_url_not_auto_person_without_profile_php():
    """Page URLs without profile.php should not auto-delete in Phase 1."""
    result = heuristic_screen(
        "John Smith",
        "https://www.facebook.com/johnsmithpage",
    )
    assert result.entity_type == ""


def test_entity_activity_constants():
    assert LEAD_ACTIVITY_ENTITY_BUSINESS == "entity_business"
    assert LEAD_ACTIVITY_ENTITY_UNCERTAIN == "entity_uncertain"


def test_pending_scrape_rows_are_included_in_work_queue():
    """pending_scrape rows must be screened (not skipped)."""
    from app.entity.screen import EntityScreenService, _SCREENED_TAGS
    from app.entity.constants import LEAD_ACTIVITY_PENDING_SCRAPE

    assert LEAD_ACTIVITY_PENDING_SCRAPE not in _SCREENED_TAGS

    rows = [
        (2, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=1",
            "Business Name": "Test Biz",
            "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
        }),
        (3, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=2",
            "Business Name": "Done",
            "Lead Activity": "entity_business",
        }),
        (4, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=3",
            "Business Name": "Retry Me",
            "Lead Activity": "entity_uncertain",
        }),
    ]
    service = EntityScreenService.__new__(EntityScreenService)
    work = service._build_work_items(rows)
    assert len(work) == 2
    assert work[0][0] == 2
    assert work[1][0] == 4


def test_needs_reconcile_skips_entity_tags():
    from app.entity.screen import _needs_reconcile_tag

    assert not _needs_reconcile_tag("entity_business")
    assert not _needs_reconcile_tag("entity_uncertain")
    assert _needs_reconcile_tag("pending_scrape")
    assert _needs_reconcile_tag("")


def test_mike_tillett_profile_is_heuristic_person():
    result = heuristic_screen(
        "Mike Tillett",
        "https://www.facebook.com/profile.php?id=1238850348",
    )
    assert result.entity_type == "person"
    assert result.confidence >= 0.88


def test_survivor_tags_are_business_or_uncertain_only():
    assert LEAD_ACTIVITY_ENTITY_BUSINESS in _ENTITY_TAGS
    assert LEAD_ACTIVITY_ENTITY_UNCERTAIN in _ENTITY_TAGS
    assert len(_ENTITY_TAGS) == 2
    assert "entity_person" not in _ENTITY_TAGS
    assert "pending_scrape" not in _ENTITY_TAGS


def test_reconcile_into_pending_tags_stragglers():
    from app.entity.constants import LEAD_ACTIVITY_PENDING_SCRAPE
    from app.entity.screen import EntityScreenService

    rows = [
        (2, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=1",
            "Business Name": "Still Pending",
            "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
        }),
        (3, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=2",
            "Business Name": "Done",
            "Lead Activity": "entity_business",
        }),
        (4, {
            "Facebook Link": "https://www.facebook.com/profile.php?id=3",
            "Business Name": "Uncertain",
            "Lead Activity": "entity_uncertain",
        }),
        (5, {
            "Facebook Link": "",
            "Business Name": "No Link",
            "Lead Activity": LEAD_ACTIVITY_PENDING_SCRAPE,
        }),
    ]
    service = EntityScreenService.__new__(EntityScreenService)
    pending_tags: dict[int, str] = {}
    to_delete_links: set[str] = set()
    stats = {
        "reconciled_uncertain": 0,
        "tagged_uncertain": 0,
    }
    service._reconcile_into_pending(rows, pending_tags, to_delete_links, stats)
    assert pending_tags == {2: LEAD_ACTIVITY_ENTITY_UNCERTAIN}
    assert stats["reconciled_uncertain"] == 1
