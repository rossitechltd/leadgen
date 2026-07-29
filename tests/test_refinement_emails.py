"""Tests for domain-aware email resolution during refinement."""

from unittest.mock import MagicMock, patch

from app.refinement.emails import (
    collect_website_domains,
    find_emails_for_domains,
    pick_best_email,
    resolve_profile_email,
)
from app.refinement.extractor import ExtractedProfile
from app.refinement.service import ProfileRefinementService


TIMBER_SCRAPE = """
Timber and Hardcore Construction
Links
timberandhardcoreconstruction.com
info@timberandhardcoreconstruction.com
Contact us for decking and fencing.
"""


def test_collect_domains_from_scrape_and_website_link():
    domains = collect_website_domains(
        TIMBER_SCRAPE,
        website_link="timberandhardcoreconstruction.com",
    )
    assert "timberandhardcoreconstruction.com" in domains


def test_resolve_timber_email_when_llm_empty():
    email = resolve_profile_email(
        "",
        scrape_text=TIMBER_SCRAPE,
        website_link="timberandhardcoreconstruction.com",
    )
    assert email == "info@timberandhardcoreconstruction.com"


def test_resolve_prefers_scrape_domain_email_over_wrong_llm():
    email = resolve_profile_email(
        "wrong@gmail.com",
        scrape_text=TIMBER_SCRAPE,
        website_link="timberandhardcoreconstruction.com",
    )
    assert email == "info@timberandhardcoreconstruction.com"


def test_resolve_keeps_matching_llm_email():
    email = resolve_profile_email(
        "info@timberandhardcoreconstruction.com",
        scrape_text=TIMBER_SCRAPE,
        website_link="timberandhardcoreconstruction.com",
    )
    assert email == "info@timberandhardcoreconstruction.com"


def test_pick_best_email_prefers_info_over_sales():
    chosen = pick_best_email(
        [
            "sales@example.co.uk",
            "info@example.co.uk",
        ]
    )
    assert chosen == "info@example.co.uk"


def test_resolve_multiple_emails_prefers_info():
    scrape = (
        "Links\nexample.co.uk\n"
        "sales@example.co.uk contact@example.co.uk info@example.co.uk"
    )
    email = resolve_profile_email(
        "",
        scrape_text=scrape,
        website_link="example.co.uk",
    )
    assert email == "info@example.co.uk"


def test_resolve_empty_when_no_domain_and_no_emails():
    email = resolve_profile_email(
        "someone@gmail.com",
        scrape_text="No website here, just a phone number.",
        website_link="",
    )
    assert email == ""


def test_find_emails_for_domains_matches_normalized_hosts():
    domains = {"timberandhardcoreconstruction.com"}
    found = find_emails_for_domains(
        "Email info@www.timberandhardcoreconstruction.com today",
        domains,
    )
    assert found == ["info@www.timberandhardcoreconstruction.com"]


def test_refine_one_uses_resolved_email_in_refined_text():
    service = ProfileRefinementService(MagicMock())
    extracted = ExtractedProfile(
        business_name="Timber and Hardcore Construction",
        business_type="Construction",
        location="Lincolnshire",
        website_link="timberandhardcoreconstruction.com",
        email="",
        description="Decking and fencing",
    )

    with patch(
        "app.refinement.service.extract_profile_fields",
        return_value=extracted,
    ):
        refined = service._refine_one(
            business_name="Timber and Hardcore Construction",
            scrape_text=TIMBER_SCRAPE,
        )

    assert "info@timberandhardcoreconstruction.com" in refined.refined_text
    assert "Email: info@timberandhardcoreconstruction.com" in refined.refined_text
