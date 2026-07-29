"""Tests for qualify website checks."""

from app.qualify.website import (
    _looks_placeholder,
    check_website,
    html_to_text,
)

PINNACLE_HTML = """
<html><head><title>Pinnacle Builders — Leeds</title></head>
<body>
<h1>A new site is on its way.</h1>
<p>We&rsquo;re rebuilding pinnaclepd.co.uk with the same care we put into our houses.
In the meantime, get in touch &mdash; we&rsquo;d love to hear about your project.</p>
<p>Call 07931 267191 WhatsApp</p>
</body></html>
"""

FULL_BUSINESS_HTML = """
<html><head><title>Joe's Plumbing Leeds</title></head>
<body>
<h1>Joe's Plumbing</h1>
<p>Our services include emergency plumbing, bathroom fitting, and heating repairs.</p>
<p>Contact us today for a free quote. Phone 0113 496 0000. email@example.com</p>
<p>About us: Family run business serving Leeds for 20 years.</p>
<p>Opening hours Monday to Friday 8am-6pm. Book online now.</p>
<p>We offer boiler installation, leak repair, and drain clearing across West Yorkshire.</p>
</body></html>
"""


def test_pinnacle_placeholder_not_genuine():
    text = html_to_text(PINNACLE_HTML)
    assert _looks_placeholder(PINNACLE_HTML, text)

    result = check_website(
        "https://pinnaclepd.co.uk",
        timeout=5.0,
        business_name="Pinnacle Builders Leeds",
        api_key="",
        model="",
        base_url="",
    )
    # Without mocking fetch, may be unreachable in CI — test heuristic path with mock
    from unittest.mock import patch

    with patch("app.qualify.website._fetch_html", return_value=(200, PINNACLE_HTML)):
        result = check_website(
            "https://pinnaclepd.co.uk",
            timeout=5.0,
            business_name="Pinnacle Builders Leeds",
            api_key="",
            model="",
            base_url="",
        )
    assert result.status.value == "expired"


SPIRE_LIKE_HTML = """
<html><head><title>The Spire Music Academy | Join, Learn, Perform Today!</title></head>
<body>
<h1>A Music Academy in the Heart of Plymouth</h1>
<p>Reach Out To Learn More. Enroll Now. Start Your Journey Here! Explore Our Music Programs.</p>
<h2>Private Lessons</h2>
<p>Private Piano program offers individualized instruction. Private Voice Lessons. Jazz Piano Lessons.</p>
<h2>Music Writing & Production</h2>
<p>Music Writing & Production for Singer-Songwriters. Digital audio workstations.</p>
<h2>Ensembles & Programs</h2>
<p>Spire Children's Chorus audition required. Spire Jazz Ensemble. Ensemble Coaching.</p>
<h2>Tuition & Policies</h2>
<p>Tuition reflects artistic excellence. Enrollment is limited. Contact us to reserve your spot.</p>
<p>781.738.9698 Dona@spiremusicacademy.com 25 1/2 Court St. Plymouth, MA 02360</p>
<p>About The Spire Academy offers music lessons and performance-based programs for children, teens, and adults.</p>
<p>Led by Accomplished Musicians. Private lessons in piano, voice, chorus, jazz ensemble, songwriting.</p>
""" + ("Program details and lesson descriptions. " * 120) + """
<p>Performance Opportunities on the Spire Stage. Book online. Our services include enrollment and tuition.</p>
</body></html>
"""


def test_spire_like_site_is_genuine():
    text = html_to_text(SPIRE_LIKE_HTML)
    assert not _looks_placeholder(SPIRE_LIKE_HTML, text)

    from unittest.mock import patch

    with patch("app.qualify.website._fetch_html", return_value=(200, SPIRE_LIKE_HTML)):
        result = check_website(
            "https://spiremusicacademy.com",
            timeout=5.0,
            business_name="Spire Music Academy",
            api_key="",
            model="",
            base_url="",
        )
    assert result.status.value == "genuine"


def test_scrape_confirms_not_fooled_by_followers_only():
    from app.qualify.website import scrape_confirms_business_website

    scrape = "Pinnacle Builders · pinnaclepd.co.uk · 500 followers · Contact info"
    assert not scrape_confirms_business_website(scrape, "https://pinnaclepd.co.uk")


def test_full_business_site_heuristic_genuine_without_ai():
    text = html_to_text(FULL_BUSINESS_HTML)
    assert not _looks_placeholder(FULL_BUSINESS_HTML, text)

    from unittest.mock import patch

    with patch("app.qualify.website._fetch_html", return_value=(200, FULL_BUSINESS_HTML)):
        result = check_website(
            "https://joesplumbing.co.uk",
            timeout=5.0,
            business_name="Joe's Plumbing",
            api_key="",
            model="",
            base_url="",
        )
    assert result.status.value == "genuine"


def test_jmd_wix_site_uses_scrape_fallback():
    wix_shell = "<html><body>" + ".c0tWWH{cursor:pointer}" * 500 + "wixstatic.com</body></html>"
    scrape = (
        "JMD Teaching · jmdteaching.co.uk · Music Lessons in Plymouth · "
        "Professional Music Tuition for Schools & Families · drums piano guitar vocals · "
        "lessons in schools workshops after-school clubs · Jamie Dobson founder · "
        "Contact jmdteaching.enquiries@gmail.com 07860 258574 · Meet the Team · "
        "Drum Teacher Guitar Teacher Piano Teacher · tuition for schools and families"
    )
    refined = (
        "Phone: 07860258574\n"
        "Website: jmdteaching.co.uk\n"
        "Music tuition drums piano guitar vocals for schools and families."
    )

    from unittest.mock import patch

    with patch("app.qualify.website._fetch_html", return_value=(200, wix_shell)):
        result = check_website(
            "https://jmdteaching.co.uk",
            timeout=5.0,
            business_name="JMD Teaching",
            api_key="",
            model="",
            base_url="",
            scrape_text=scrape,
            refined_text=refined,
        )
    assert result.status.value == "genuine"


def test_pinnacle_extracts_phone_from_placeholder_page():
    from unittest.mock import patch

    with patch("app.qualify.website._fetch_html", return_value=(200, PINNACLE_HTML)):
        result = check_website(
            "https://pinnaclepd.co.uk",
            timeout=5.0,
            business_name="Pinnacle Builders Leeds",
            api_key="",
            model="",
            base_url="",
        )
    assert result.status.value == "expired"
    assert result.page_phones


def test_ai_expired_with_placeholder_page_keeps_lead():
    from unittest.mock import patch

    from app.qualify.website_classifier import WebsiteClassifyResult

    text = html_to_text(PINNACLE_HTML)
    with patch("app.qualify.website._fetch_html", return_value=(200, PINNACLE_HTML)):
        with patch(
            "app.qualify.website.classify_website_html",
            return_value=WebsiteClassifyResult(
                status="expired_or_parked",
                reason="placeholder page",
            ),
        ):
            result = check_website(
                "https://pinnaclepd.co.uk",
                timeout=5.0,
                api_key="test-key",
                model="test",
                base_url="https://openrouter.ai/api/v1",
            )
    assert result.status.value == "expired"


def test_ai_wrong_expired_on_full_business_site_is_genuine():
    from unittest.mock import patch

    from app.qualify.website_classifier import WebsiteClassifyResult

    with patch("app.qualify.website._fetch_html", return_value=(200, FULL_BUSINESS_HTML)):
        with patch(
            "app.qualify.website.classify_website_html",
            return_value=WebsiteClassifyResult(
                status="expired_or_parked",
                reason="simple splash page",
            ),
        ):
            result = check_website(
                "https://joesplumbing.co.uk",
                timeout=5.0,
                api_key="test-key",
                model="test",
                base_url="https://openrouter.ai/api/v1",
                business_name="Joe's Plumbing",
            )
    assert result.status.value == "genuine"


def test_ai_unavailable_full_business_site_is_genuine():
    from unittest.mock import patch

    from app.qualify.website_classifier import WebsiteClassifyResult

    with patch("app.qualify.website._fetch_html", return_value=(200, FULL_BUSINESS_HTML)):
        with patch(
            "app.qualify.website.classify_website_html",
            return_value=WebsiteClassifyResult(
                status="unavailable",
                reason="could not determine",
            ),
        ):
            result = check_website(
                "https://joesplumbing.co.uk",
                timeout=5.0,
                api_key="test-key",
                model="test",
                base_url="https://openrouter.ai/api/v1",
                business_name="Joe's Plumbing",
            )
    assert result.status.value == "genuine"


def test_qualify_service_rejects_active_website():
    from unittest.mock import MagicMock, patch

    from app.qualify.service import AIQualifyService
    from app.qualify.website_status import WebsiteStatusCode, WebsiteStatusResult
    from app.sheets.columns import COL_PHONE_1, COL_REFINED, COL_SCRAPE, COL_WEBSITE_LINK

    settings = MagicMock()
    settings.qualify_website_timeout_secs = 5.0
    settings.qualify_max_redirects = 10
    settings.qualify_fetch_retries = 3

    service = AIQualifyService(settings)
    row = {
        COL_PHONE_1: "07123456789",
        COL_WEBSITE_LINK: "joesplumbing.co.uk",
        COL_SCRAPE: "Joe's Plumbing · joesplumbing.co.uk · plumbing services Leeds",
        COL_REFINED: "• Phone: 07123456789",
    }

    active_status = WebsiteStatusResult(
        status=WebsiteStatusCode.ACTIVE,
        reason="Functioning standalone business website detected.",
        qualified=False,
        original_url="joesplumbing.co.uk",
    )

    with patch.object(service, "_classify_website", return_value=active_status):
        evaluation = service._evaluate_row(row)

    assert not evaluation.decision.keep
    assert "active" in evaluation.decision.reason.lower()


def test_qualify_service_rejects_parked_website():
    from unittest.mock import MagicMock, patch

    from app.qualify.service import AIQualifyService
    from app.qualify.website_status import WebsiteStatusCode, WebsiteStatusResult
    from app.sheets.columns import COL_PHONE_1, COL_REFINED, COL_SCRAPE, COL_WEBSITE_LINK

    settings = MagicMock()
    settings.qualify_website_timeout_secs = 5.0
    settings.qualify_max_redirects = 10
    settings.qualify_fetch_retries = 3

    service = AIQualifyService(settings)
    row = {
        COL_PHONE_1: "07123456789",
        COL_WEBSITE_LINK: "https://business.co.uk",
        COL_SCRAPE: "Business page",
        COL_REFINED: "• Phone: 07123456789",
    }

    parked_status = WebsiteStatusResult(
        status=WebsiteStatusCode.PARKED,
        reason="Domain appears parked or showing a generic placeholder page.",
        qualified=False,
        original_url="https://business.co.uk",
    )

    with patch.object(service, "_classify_website", return_value=parked_status):
        evaluation = service._evaluate_row(row)

    assert not evaluation.decision.keep
    assert "parked" in evaluation.decision.reason.lower()


def test_qualify_service_final_sweep_removes_marked_active_rows():
    from unittest.mock import MagicMock, patch

    from app.qualify.service import AIQualifyService
    from app.qualify.website_status import WebsiteStatusCode, WebsiteStatusResult
    from app.sheets.columns import COL_BUSINESS_NAME, COL_PHONE_1, COL_SCRAPE, COL_WEBSITE_STATUS

    settings = MagicMock()
    settings.qualify_website_timeout_secs = 5.0
    settings.qualify_max_redirects = 10
    settings.qualify_fetch_retries = 3
    settings.sheets_configured = True
    settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    settings.openrouter_configured = True

    service = AIQualifyService(settings)
    kept_row = {
        COL_BUSINESS_NAME: "Good Lead",
        COL_SCRAPE: "some scrape",
        COL_PHONE_1: "07123456789",
        COL_WEBSITE_STATUS: "NO_WEBSITE",
    }
    active_row = {
        COL_BUSINESS_NAME: "Active Site",
        COL_SCRAPE: "scrape",
        COL_WEBSITE_STATUS: "ACTIVE",
    }
    initial_rows = [(10, kept_row), (20, active_row)]
    post_sweep_rows = [(10, kept_row)]

    no_website = WebsiteStatusResult(
        status=WebsiteStatusCode.NO_WEBSITE,
        reason="No website",
        qualified=True,
    )

    delete_calls: list[list[int]] = []

    def capture_delete(sheet, indices):
        delete_calls.append(list(indices))

    with (
        patch("sheets.ensure_worksheet"),
        patch("sheets.extend_worksheet_headers"),
        patch("sheets.invalidate_worksheet_cache"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            side_effect=[initial_rows, post_sweep_rows],
        ),
        patch("sheets.batch_update_rows_by_header"),
        patch("sheets.delete_rows", side_effect=capture_delete),
        patch.object(service, "_classify_website", return_value=no_website) as mock_classify,
    ):
        result = service.run()

    assert result.ok
    assert result.stats["removed"] == 1
    assert result.stats["kept"] == 1
    assert delete_calls == [[20]]
    mock_classify.assert_called_once()


def test_qualify_service_final_sweep_removes_leftover_active_rows():
    from unittest.mock import MagicMock, patch

    from app.qualify.service import AIQualifyService
    from app.sheets.columns import COL_BUSINESS_NAME, COL_SCRAPE, COL_WEBSITE_STATUS

    settings = MagicMock()
    settings.qualify_website_timeout_secs = 5.0
    settings.qualify_max_redirects = 10
    settings.qualify_fetch_retries = 3
    settings.sheets_configured = True
    settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    settings.openrouter_configured = True

    service = AIQualifyService(settings)
    active_row = {
        COL_BUSINESS_NAME: "Leftover Active",
        COL_SCRAPE: "scrape",
        COL_WEBSITE_STATUS: "ACTIVE",
    }
    rows_with_active = [(15, active_row)]

    delete_calls: list[list[int]] = []

    def capture_delete(sheet, indices):
        delete_calls.append(list(indices))

    with (
        patch("sheets.ensure_worksheet"),
        patch("sheets.extend_worksheet_headers"),
        patch("sheets.invalidate_worksheet_cache"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            side_effect=[rows_with_active, rows_with_active],
        ),
        patch("sheets.batch_update_rows_by_header"),
        patch("sheets.delete_rows", side_effect=capture_delete),
    ):
        result = service.run()

    assert result.ok
    assert result.stats["sweep_removed_active"] == 1
    assert delete_calls == [[15], [15]]


def test_qualify_service_writes_status_and_deletes_active_website():
    from unittest.mock import MagicMock, patch

    from app.qualify.service import AIQualifyService
    from app.qualify.website_status import WebsiteStatusCode, WebsiteStatusResult
    from app.sheets.columns import (
        COL_PHONE_1,
        COL_SCRAPE,
        COL_VA,
        COL_WEBSITE_LINK,
        COL_WEBSITE_STATUS,
    )

    settings = MagicMock()
    settings.qualify_website_timeout_secs = 5.0
    settings.qualify_max_redirects = 10
    settings.qualify_fetch_retries = 3
    settings.sheets_configured = True
    settings.sheet_dynamic_lead = "Dynamic Lead Sheet"
    settings.openrouter_configured = True

    service = AIQualifyService(settings)
    row = {
        COL_PHONE_1: "07123456789",
        COL_WEBSITE_LINK: "https://pow-powerofwomen.co.uk",
        COL_SCRAPE: "POW connects women led businesses across Dorset",
    }

    active_status = WebsiteStatusResult(
        status=WebsiteStatusCode.ACTIVE,
        reason="Functioning standalone business website detected.",
        qualified=False,
        original_url="https://pow-powerofwomen.co.uk",
    )

    rows = [(13, row)]
    pending: dict[int, dict] = {}
    call_order: list[str] = []

    def capture_batch(sheet, updates):
        pending.update(updates)
        call_order.append("update")

    def capture_delete(sheet, indices):
        call_order.append("delete")

    with (
        patch("sheets.ensure_worksheet"),
        patch("sheets.extend_worksheet_headers"),
        patch("sheets.invalidate_worksheet_cache"),
        patch(
            "sheets.read_rows_with_sheet_indices",
            side_effect=[rows, []],
        ),
        patch("sheets.batch_update_rows_by_header", side_effect=capture_batch),
        patch("sheets.delete_rows", side_effect=capture_delete),
        patch.object(service, "_classify_website", return_value=active_status),
    ):
        result = service.run()

    assert result.ok
    assert result.stats["removed"] == 1
    assert 13 in pending
    assert pending[13][COL_WEBSITE_STATUS] == "ACTIVE"
    assert COL_VA not in pending[13]
    assert call_order == ["update", "delete"]
