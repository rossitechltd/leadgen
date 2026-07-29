"""Tests for outreach message templates and assignment."""

from unittest.mock import patch

from app.outreach.messages import (
    OUTREACH_TEMPLATES,
    build_outreach_message,
    resolve_first_name,
)
from app.outreach.service import OutreachMessageService
from app.sheets.columns import (
    COL_BUSINESS_NAME,
    COL_BUSINESS_OWNER,
    COL_MESSAGE_1,
    COL_REFINED,
    COL_SCRAPE,
    COL_VA,
    COL_WEBSITE_STATUS,
)


def test_resolve_first_name_from_owner():
    row = {COL_BUSINESS_OWNER: "Samantha Tarr"}
    assert resolve_first_name(row) == "Samantha"


def test_resolve_first_name_missing_uses_there():
    row = {COL_BUSINESS_OWNER: ""}
    assert resolve_first_name(row) == "there"


def test_resolve_first_name_notfound_uses_there():
    row = {COL_BUSINESS_OWNER: "notfound"}
    assert resolve_first_name(row) == "there"


def test_resolve_first_name_strips_honorific():
    row = {COL_BUSINESS_OWNER: "Dr. James Smith"}
    assert resolve_first_name(row) == "James"


def test_build_outreach_message_substitutes_firstname():
    msg = build_outreach_message("Amy", template=OUTREACH_TEMPLATES[0])
    assert "Hi Amy!" in msg
    assert "{firstname}" not in msg


def test_build_outreach_message_uses_there():
    msg = build_outreach_message("there", template=OUTREACH_TEMPLATES[0])
    assert msg.startswith("Hi there!")


def test_outreach_target_includes_scraped_lead_without_va():
    service = OutreachMessageService(type("S", (), {})())
    row = {
        COL_SCRAPE: "Facebook page text",
        COL_VA: "",
        COL_WEBSITE_STATUS: "NO_WEBSITE",
    }
    assert service._is_outreach_target(row)


def test_outreach_target_excludes_remove_status():
    service = OutreachMessageService(type("S", (), {})())
    row = {
        COL_SCRAPE: "text",
        COL_WEBSITE_STATUS: "ACTIVE",
    }
    assert not service._is_outreach_target(row)


def test_outreach_service_writes_message1_for_outreach_targets():
    settings = type(
        "S",
        (),
        {
            "sheets_configured": True,
            "sheet_dynamic_lead": "Dynamic Lead Sheet",
        },
    )()
    service = OutreachMessageService(settings)
    rows = [
        (
            5,
            {
                COL_VA: "qualified",
                COL_BUSINESS_OWNER: "John Smith",
                COL_BUSINESS_NAME: "John's Plumbing",
                COL_SCRAPE: "page text",
                COL_WEBSITE_STATUS: "NO_WEBSITE",
            },
        ),
        (
            6,
            {
                COL_VA: "",
                COL_BUSINESS_OWNER: "Jane",
                COL_BUSINESS_NAME: "Jane Ltd",
                COL_SCRAPE: "page text",
                COL_WEBSITE_STATUS: "DEAD_DOMAIN",
            },
        ),
    ]
    rows_after = [
        (
            5,
            {
                **rows[0][1],
                COL_MESSAGE_1: "Hi John! Found your Facebook page",
            },
        ),
        (
            6,
            {
                **rows[1][1],
                COL_MESSAGE_1: "Hi Jane! Found your Facebook page",
            },
        ),
    ]
    pending: dict[int, dict] = {}

    with (
        patch("sheets.ensure_worksheet"),
        patch("sheets.extend_worksheet_headers"),
        patch("sheets.invalidate_worksheet_cache"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            side_effect=[rows, rows_after],
        ),
        patch(
            "sheets.batch_update_rows_by_header",
            side_effect=lambda sheet, updates: pending.update(updates),
        ),
        patch("app.outreach.messages.random.choice", return_value=OUTREACH_TEMPLATES[0]),
    ):
        result = service.run()

    assert result.ok
    assert result.stats["updated"] == 2
    assert 5 in pending
    assert 6 in pending
    assert pending[6][COL_VA] == "qualified"
    assert "Hi John!" in pending[5]["Message1"]
    assert "Hi Jane!" in pending[6]["Message1"]


def test_sweep_missing_messages_direct():
    settings = type(
        "S",
        (),
        {
            "sheets_configured": True,
            "sheet_dynamic_lead": "Dynamic Lead Sheet",
        },
    )()
    service = OutreachMessageService(settings)
    row = {
        COL_SCRAPE: "text",
        COL_BUSINESS_OWNER: "Amy",
        COL_WEBSITE_STATUS: "NO_WEBSITE",
    }
    stats: dict = {"updated": 0, "with_name": 0, "with_there": 0, "va_backfilled": 0, "sweep_filled": 0}
    pending: dict[int, dict] = {}

    with (
        patch("sheets.invalidate_worksheet_cache"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            return_value=[(12, row)],
        ),
        patch(
            "sheets.batch_update_rows_by_header",
            side_effect=lambda sheet, updates: pending.update(updates),
        ),
        patch("app.outreach.messages.random.choice", return_value=OUTREACH_TEMPLATES[0]),
    ):
        swept = service._sweep_missing_messages(settings.sheet_dynamic_lead, stats)

    assert swept == 1
    assert stats["sweep_filled"] == 1
    assert "Hi Amy!" in pending[12][COL_MESSAGE_1]
