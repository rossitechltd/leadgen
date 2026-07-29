"""Tests for lead list upload parsing."""

from app.leads.parse_upload import parse_lead_list


def test_csv_without_headers_link_then_name():
    text = (
        "https://www.facebook.com/acmeplumbing, Acme Plumbing\n"
        "https://www.facebook.com/joes-hvac, Joe's HVAC\n"
    )
    leads = parse_lead_list(text)
    assert len(leads) == 2
    assert leads[0]["business_name"] == "Acme Plumbing"
    assert leads[1]["business_name"] == "Joe's HVAC"


def test_csv_without_headers_name_then_link():
    text = "Acme Plumbing, https://www.facebook.com/acmeplumbing\n"
    leads = parse_lead_list(text)
    assert len(leads) == 1
    assert leads[0]["business_name"] == "Acme Plumbing"
    assert "acmeplumbing" in leads[0]["facebook_link"]


def test_csv_with_headers():
    text = (
        "Facebook Link,Business Name\n"
        "https://www.facebook.com/testbiz, Test Business LLC\n"
    )
    leads = parse_lead_list(text)
    assert len(leads) == 1
    assert leads[0]["business_name"] == "Test Business LLC"


def test_tsv_without_headers():
    text = (
        "https://www.facebook.com/page1\tBiz One\n"
        "https://www.facebook.com/page2\tBiz Two\n"
    )
    leads = parse_lead_list(text)
    assert leads[0]["business_name"] == "Biz One"
    assert leads[1]["business_name"] == "Biz Two"


def test_comma_in_business_name():
    text = "https://www.facebook.com/test, Joe's Plumbing, LLC\n"
    leads = parse_lead_list(text)
    assert len(leads) == 1
    assert leads[0]["business_name"] == "Joe's Plumbing, LLC"


def test_facebook_export_metadata_columns():
    text = (
        "https://www.facebook.com/sparkpro,Sparkpro Carpet Cleaning,Joined 19 hours ago,"
        "Carpet cleaner, ,2 people follow this, ,Follow\n"
        "https://www.facebook.com/pinnacle,Pinnacle Builders - Leeds,Joined 19 hours ago,"
        "Contractor, ,41631 people follow this, ,Follow\n"
    )
    leads = parse_lead_list(text)
    assert len(leads) == 2
    assert leads[0]["business_name"] == "Sparkpro Carpet Cleaning"
    assert leads[1]["business_name"] == "Pinnacle Builders - Leeds"


def test_facebook_metadata_in_single_cell():
    text = (
        "https://www.facebook.com/prime, Prime Touch, Joined 20 hours ago, House painting "
        "· 2 people follow this · Follow\n"
    )
    leads = parse_lead_list(text)
    assert len(leads) == 1
    assert leads[0]["business_name"] == "Prime Touch"
