"""Configurable hostname lists for website status classification."""

from __future__ import annotations

# Social media platforms — redirect to these = SOCIAL_REDIRECT (qualified)
SOCIAL_PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "facebook.com",
        "fb.com",
        "instagram.com",
        "tiktok.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "youtu.be",
        "pinterest.com",
        "threads.net",
        "snapchat.com",
    }
)

# Business directories — redirect to these = DIRECTORY_REDIRECT (qualified)
DIRECTORY_PLATFORM_HOSTS: frozenset[str] = frozenset(
    {
        "yell.com",
        "checkatrade.com",
        "yelp.com",
        "trustpilot.com",
        "thomsonlocal.com",
        "freeindex.co.uk",
        "bark.com",
        "ratedpeople.com",
        "mybuilder.com",
        "google.com",
        "maps.google.com",
        "business.google.com",
        "bing.com",
        "places.bing.com",
        "cylex.co.uk",
        "hotfrog.co.uk",
        "192.com",
        "locallife.co.uk",
        "touchlocal.com",
        "find-open.co.uk",
    }
)

# Known domain marketplace / parking providers (content patterns used separately)
DOMAIN_MARKETPLACE_HINTS: frozenset[str] = frozenset(
    {
        "sedoparking.com",
        "dan.com",
        "afternic.com",
        "hugedomains.com",
        "godaddy.com",
        "namecheap.com",
        "sedo.com",
    }
)
