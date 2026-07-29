"""Column definitions for Lead Manager sheets."""

COL_FACEBOOK_LINK = "Facebook Link"
COL_BUSINESS_NAME = "Business Name"
COL_PHONE_1 = "Phone Number 1"
COL_PHONE_2 = "Phone Number 2"
COL_BUSINESS_OWNER = "Business Owner"
COL_WEBSITE_SCRAPE = "Website Link Scrape"
COL_LEAD_ACTIVITY = "Lead Activity"
COL_MESSAGE_1 = "Message1"
COL_MESSAGE_2 = "Message2"
COL_VA = "va"
COL_REFINED = "refined"

DYNAMIC_LEAD_HEADERS: list[str] = [
    COL_FACEBOOK_LINK,
    COL_BUSINESS_NAME,
    COL_PHONE_1,
    COL_PHONE_2,
    COL_BUSINESS_OWNER,
    COL_WEBSITE_SCRAPE,
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
LEAD_ACTIVITY_FAILED = "scrape_failed"

# scrapesheet tab (one lead at row 2 for MMM) — columns: link, data
COL_SCRAPE_LINK = "link"
COL_SCRAPE_DATA = "data"

SCRAPE_SHEET_HEADERS: list[str] = [COL_SCRAPE_LINK, COL_SCRAPE_DATA]

SCRAPE_SHEET_ROW = 2

# allimported sheet link column for dedupe matching
ALL_IMPORTED_LINK_COLUMN = "link"

# Ready to Contact uses the same columns as Dynamic Lead Sheet
READY_TO_CONTACT_HEADERS: list[str] = DYNAMIC_LEAD_HEADERS.copy()
