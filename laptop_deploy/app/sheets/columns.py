"""Column definitions for Lead Manager sheets."""

COL_FACEBOOK_LINK = "Facebook Link"
COL_BUSINESS_NAME = "Business Name"
COL_PHONE_1 = "Phone Number 1"
COL_PHONE_2 = "Phone Number 2"
COL_BUSINESS_OWNER = "Business Owner"
# Facebook page text from Step 3 (actual sheet header is "Scrape")
COL_SCRAPE = "Scrape"
COL_WEBSITE_LINK = "Website Link"
COL_LEAD_ACTIVITY = "Lead Activity"
COL_MESSAGE_1 = "Message1"
COL_MESSAGE_2 = "Message2"
COL_VA = "va"
COL_REFINED = "refined"

# Legacy name used in older docs/code paths
COL_WEBSITE_SCRAPE = COL_SCRAPE

DYNAMIC_LEAD_HEADERS: list[str] = [
    COL_FACEBOOK_LINK,
    COL_BUSINESS_NAME,
    COL_SCRAPE,
    COL_PHONE_1,
    COL_PHONE_2,
    COL_BUSINESS_OWNER,
    COL_WEBSITE_LINK,
    COL_LEAD_ACTIVITY,
    COL_MESSAGE_1,
    COL_MESSAGE_2,
    COL_VA,
    COL_REFINED,
]

# Lead Activity values used by Step 3
LEAD_ACTIVITY_PENDING = "pending_scrape"
LEAD_ACTIVITY_SCRAPING = "scraping"
LEAD_ACTIVITY_SCRAPED = "scraped"
LEAD_ACTIVITY_FAILED_1 = "scrape_failed_1"
LEAD_ACTIVITY_FAILED_2 = "scrape_failed_2"
LEAD_ACTIVITY_FAILED_3 = "scrape_failed_3"
# Permanent final state (alias for scrape_failed_3)
LEAD_ACTIVITY_FAILED = LEAD_ACTIVITY_FAILED_3


def scrape_failed_activity_label(failure_count: int, max_failures: int = 3) -> str:
    """Map failure round 1..max_failures to scrape_failed_N in Lead Activity."""
    attempt = min(max(1, failure_count), max_failures)
    return f"scrape_failed_{attempt}"


def is_scrape_failed_activity(activity: str) -> bool:
    act = (activity or "").strip().lower()
    if act == "scrape_failed":
        return True
    if not act.startswith("scrape_failed_"):
        return False
    suffix = act[len("scrape_failed_"):]
    return suffix.isdigit() and int(suffix) >= 1

# scrapesheet tab (row 2) — two columns only; MMM triggers when link (A) changes
COL_SCRAPE_LINK = "link"
COL_SCRAPE_DATA = "data"

SCRAPE_SHEET_HEADERS: list[str] = [
    COL_SCRAPE_LINK,
    COL_SCRAPE_DATA,
]

SCRAPE_SHEET_ROW = 2

# allimported sheet link column for dedupe matching
ALL_IMPORTED_LINK_COLUMN = "link"

# Ready to Contact uses the same columns as Dynamic Lead Sheet
READY_TO_CONTACT_HEADERS: list[str] = DYNAMIC_LEAD_HEADERS.copy()
