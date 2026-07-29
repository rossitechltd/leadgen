"""Tests for deterministic website status classification."""

from unittest.mock import patch

import pytest

from app.qualify.website_status import (
    WebsiteStatusCode,
    classify_website_link,
)


def _mock_fetch(status_code: int, html: str, final_url: str, chain: tuple[str, ...] = ()):
    from app.qualify.website_status import _FetchResult

    def _fetch(url, **kwargs):
        full_chain = chain or (url, final_url)
        return _FetchResult(
            ok=True,
            status_code=status_code,
            html=html,
            final_url=final_url,
            redirect_chain=full_chain,
        )

    return _fetch


FULL_BUSINESS_HTML = """
<html><head><title>Joe's Plumbing Leeds</title></head>
<body>
<h1>Joe's Plumbing</h1>
<p>Our services include emergency plumbing, bathroom fitting, and heating repairs.</p>
<p>Contact us today for a free quote. Phone 0113 496 0000. email@example.com</p>
<p>About us: Family run business serving Leeds for 20 years.</p>
<p>Opening hours Monday to Friday 8am-6pm. Book online now.</p>
</body></html>
"""

WIX_BLANK_HTML = """
<html><body><script>wixstatic.com</script><div>Powered by Wix</div></body></html>
"""

SQ_BLANK_HTML = """
<html><body>static.squarespace.com Coming Soon collection-type-pre-launch</body></html>
"""

FOR_SALE_HTML = """
<html><body><h1>This domain is for sale</h1><p>Buy this domain today.</p></body></html>
"""

PARKED_HTML = """
<html><body><p>This domain is parked. Parked free courtesy of GoDaddy.</p></body></html>
"""

SOFT_404_HTML = """
<html><head><title>404 - Page Not Found</title></head>
<body><h1>Page Not Found</h1></body></html>
"""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", WebsiteStatusCode.NO_WEBSITE),
        ("   ", WebsiteStatusCode.NO_WEBSITE),
    ],
)
def test_no_website(raw, expected):
    result = classify_website_link(raw)
    assert result.status == expected
    assert result.qualified


def test_active_full_business_site():
    url = "https://joesplumbing.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, FULL_BUSINESS_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.ACTIVE
    assert not result.qualified


def test_http_404():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(404, SOFT_404_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.HTTP_404
    assert result.qualified


def test_soft_404_content():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, SOFT_404_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.HTTP_404


def test_domain_for_sale():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, FOR_SALE_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.DOMAIN_FOR_SALE


def test_parked():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, PARKED_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.PARKED
    assert not result.qualified


def test_blank_wix():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, WIX_BLANK_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.BLANK_WIX


def test_blank_squarespace():
    url = "https://business.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, SQ_BLANK_HTML, url),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.BLANK_SQUARESPACE


def test_social_redirect():
    start = "https://business.co.uk"
    final = "https://www.facebook.com/business"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, "<html></html>", final, (start, final)),
        ):
            result = classify_website_link(start)
    assert result.status == WebsiteStatusCode.SOCIAL_REDIRECT
    assert result.qualified


def test_directory_redirect_yell():
    start = "https://business.co.uk"
    final = "https://www.yell.com/biz/business-name"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, "<html></html>", final, (start, final)),
        ):
            result = classify_website_link(start)
    assert result.status == WebsiteStatusCode.DIRECTORY_REDIRECT


def test_business_website_redirect():
    start = "https://oldbusiness.co.uk"
    final = "https://newbusiness.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, FULL_BUSINESS_HTML, final, (start, final)),
        ):
            result = classify_website_link(start)
    assert result.status == WebsiteStatusCode.BUSINESS_WEBSITE_REDIRECT
    assert not result.qualified


def test_dead_domain():
    with patch("app.qualify.website_status._dns_resolves", return_value=False):
        result = classify_website_link("https://business.co.uk")
    assert result.status == WebsiteStatusCode.DEAD_DOMAIN


def test_unreachable():
    from app.qualify.website_status import _FetchResult

    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            return_value=_FetchResult(ok=False, error="timeout"),
        ):
            result = classify_website_link("https://business.co.uk")
    assert result.status == WebsiteStatusCode.UNREACHABLE


def test_url_normalization_without_protocol():
    url = "https://timberandhardcoreconstruction.com"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            _mock_fetch(200, FULL_BUSINESS_HTML, url),
        ):
            result = classify_website_link("timberandhardcoreconstruction.com")
    assert result.normalized_url.startswith("https://")
    assert result.status == WebsiteStatusCode.ACTIVE


def test_as_row_fields():
    result = classify_website_link("")
    fields = result.as_row_fields()
    assert fields["Website Status"] == "NO_WEBSITE"
    assert "Website Status Reason" in fields


def test_paving_style_ecommerce_not_manual_review():
    """Large e-commerce HTML with captcha widget strings should be ACTIVE."""
    html = (
        "<html><head><title>Natural Paving Stones Direct</title></head><body>"
        + "<h1>Porcelain Paving</h1><p>Contact us today. Our services include paving.</p>"
        + "<p>About us — nationwide delivery. Phone 0333 321 5091. Book online.</p>"
        + "<div class='g-recaptcha' data-sitekey='captcha'></div>"
        + (" Shop paving sandstone limestone products delivery nationwide. " * 200)
        + "</body></html>"
    )
    from unittest.mock import patch

    url = "https://pavingstonesdirect.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            lambda u, **kw: __import__(
                "app.qualify.website_status", fromlist=["_FetchResult"]
            )._FetchResult(
                ok=True,
                status_code=200,
                html=html,
                final_url=url,
                redirect_chain=(url,),
            ),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.ACTIVE
    assert not result.qualified


def test_concept_style_403_bot_block_is_active():
    html = "<html><body>" + ("x" * 8000) + "403 Forbidden hosting</body></html>"
    from unittest.mock import patch
    from app.qualify.website_status import _FetchResult

    url = "https://conceptclaimsolutions.co.uk"
    with patch("app.qualify.website_status._dns_resolves", return_value=True):
        with patch(
            "app.qualify.website_status._fetch_url",
            return_value=_FetchResult(
                ok=True,
                status_code=403,
                html=html,
                final_url=url,
                redirect_chain=(url,),
            ),
        ):
            result = classify_website_link(url)
    assert result.status == WebsiteStatusCode.ACTIVE
    assert not result.qualified
