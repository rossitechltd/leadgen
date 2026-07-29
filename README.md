# Lead Gen Pipeline

FastAPI dashboard and daily scheduler for a 6-step Facebook lead pipeline backed by Google Sheets (**Lead Manager**).

## Prerequisites

- Python 3.10+ (Mac for testing, Windows PC for production page scrape)
- Google Cloud service account (`autoleadverification` project)
- Spreadsheet **Lead Manager** shared with `joshua@autoleadverification.iam.gserviceaccount.com`

## Google Sheets access

All apps use the shared module [`sheets.py`](sheets.py) at the project root:

- `get_client()` — loads the service account key once
- `read_all(worksheet_name)` — rows as dicts
- `append_rows(worksheet_name, rows)` — append data
- `ensure_worksheet(name, headers)` — get or create a tab

**Credentials:** `autoleadverification-e76d53033380.json` in the project root (gitignored).

## Setup

1. **Clone / open this folder** on your Mac (testing) or Windows PC (production).

2. **Place the service account key**
   - Copy `autoleadverification-e76d53033380.json` to the project root

3. **Share the spreadsheet**
   - Open **Lead Manager** in Google Sheets
   - Share with `joshua@autoleadverification.iam.gserviceaccount.com` (Editor access)

4. **Configure environment** (optional — defaults work if key file is present)

   **Mac / Linux:**
   ```bash
   cp .env.example .env
   ```

   **Windows:**
   ```bat
   copy .env.example .env
   ```

5. **Run the app**

   **Mac / Linux (testing):**
   ```bash
   chmod +x run.sh   # first time only
   ./run.sh
   ```

   **Mac (double-click):** open `run.command` in Finder (first time: right-click → Open if Gatekeeper blocks it).

   **Windows (production):**
   ```bat
   run.bat
   ```

   Open http://127.0.0.1:8000

## Pipeline steps

| Step | Name | Status |
|------|------|--------|
| 1 | Group Scrape | **Live** — Apify → Dynamic Lead Sheet |
| 2 | Dedupe | **Live** — removes links already in `allimported` |
| 3 | Page Scrape | **Live** — enqueues to scrapesheet (link + data) |
| 4 | Refine | Stub |
| 5 | AI Qualify | Stub |
| 6 | Finalize | Stub |

**Step 2:** Compares `Facebook Link` on Dynamic Lead Sheet against **allimported** (normalized URLs) and deletes rows you have already contacted. Run after Step 1 before page scraping.

## Step 3 — scrapesheet (one lead at a time)

Step 3 uses a dedicated **scrapesheet** tab with two columns only:

| link | data |
|------|------|
| Facebook URL (Python) | Page scrape text (MMM) |

Only **row 2** is used. MMM always scrapes the same screen position.

### Flow

```
Step 1–2 (main PC) → pending leads on Dynamic Lead Sheet
Step 3 (main PC)  → copies next link to scrapesheet row 2 column A (data may carry over from prior paste)
MMM (laptop)      → when link changes: scrape, clear B, paste into data column
Poller (main PC)  → when new paste stabilizes: write to Dynamic Lead Sheet → set next link in A
```

MMM triggers **only when column A (link) changes** — not when data is empty or changes.

Failed scrapes **retry once**, then marked `scrape_failed` on Dynamic Lead Sheet.

### Main PC

Step 3 enqueues the first lead if scrapesheet row 2 `link` is empty.

### Laptop

MMM loops on **scrapesheet** row 2: wait until `link` changes → scrape → clear B → paste `data`. No Python required on the laptop.

### Main PC (automatic)

While `run.bat` / `./run.sh` is running, a background poller every ~20s watches scrapesheet:

1. When `data` has a new paste → copy to Dynamic Lead Sheet
2. Set the next pending `link` in column A (MMM starts next scrape)

You should see log lines like `Scrape queue: wrote back row N` in the terminal.

### API (Step 3 queue)

- `GET /api/step3/queue/status` — pending count, current scrapesheet row
- `POST /api/step3/queue/enqueue` — load next link into scrapesheet row 2
- `POST /api/step3/queue/finalize` — verify scrape and write back
- `POST /api/step3/queue/tick` — one worker cycle

## API endpoints

- `GET /` — Dashboard
- `GET /api/health` — Health + Sheets connectivity
- `GET /api/status` — Scheduler and pipeline status
- `GET /api/pipeline/logs` — Recent log messages
- `POST /api/pipeline/run` — Run all steps
- `POST /api/pipeline/steps/{step_id}/run` — Run a single step
- `GET /api/step3/queue/status` — Scrape Queue status
- `POST /api/step3/queue/enqueue` — Load next lead to Scrape Queue row 2

## Project structure

```
sheets.py           # Shared Google Sheets module (all apps import from here)
laptop_deploy/      # Windows laptop package (scrape worker only)
app/
  main.py           # FastAPI app
  config.py         # Environment settings
  pipeline/         # Runner + steps
  scrape_queue/     # Step 3 queue service
  sheets/           # Column defs + thin client wrapper
  templates/        # Dashboard HTML
  static/           # CSS + JS
```

## Dynamic Lead Sheet columns

`Facebook Link`, `Business Name`, `Scrape`, `Phone Number 1`, `Phone Number 2`, `Business Owner`, `Website Link`, `Lead Activity`, `Message1`, `Message2`, `va`, `refined`

## Step 1 testing (Apify Group Members)

1. **Apify token** — copy from [Apify Console → Integrations](https://console.apify.com/account/integrations) into `.env`:
   ```
   APIFY_API_TOKEN=apify_api_...
   ```

2. **Facebook cookies** — while logged into facebook.com in Chrome:
   - Install [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - Open **facebook.com** and confirm you are logged in
   - Cookie-Editor → Export → save as `credentials/fb_cookies.json` (JSON array)
   - **Re-export immediately before each scrape run** if you see "Cookies are no longer valid"
   - Use a **dedicated Facebook account** for scraping (not your main personal account)
   - **Close facebook.com in your browser** while Step 1 runs (two sessions fight each other)
   - Set `APIFY_PROXY_COUNTRY` to the country you logged in from (e.g. `GB`)
   - Keep `APIFY_PROXY_GROUPS=RESIDENTIAL` — datacenter IPs invalidate cookies within minutes

### Cookies invalid every few minutes?

Facebook kills sessions when it sees your cookies used from a different IP/type than your browser:

1. Re-export fresh cookies from Chrome **right before** running Step 1
2. Confirm `.env` has `APIFY_PROXY_COUNTRY=GB` and `APIFY_PROXY_GROUPS=RESIDENTIAL`
3. Do not stay logged into facebook.com in Chrome during the Apify run
4. Use a scraper-only Facebook account if possible
5. Restart the app after changing `.env` (`./run.command`)

3. **Group URLs** — copy and edit:
   ```bash
   cp config/groups.example.json config/groups.json
   ```
   Add your group URLs to the `groups` array.

4. **Optional test limits** in `.env`:
   ```
   APIFY_MEMBER_COUNT=20
   APIFY_PROXY_COUNTRY=US
   ```

5. **Run Step 1** from the dashboard → **Run step** on **Group Scrape**, or:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/pipeline/steps/1/run
   ```

Apify actor input uses flat keys (not nested JSON):
- `cookies` (required)
- `scrapeGroupMembers.groupUrl` (required, one group per run)
- `proxy.useApifyProxy` + `apifyProxyCountry` (required)
- `minDelay` / `maxDelay`

New leads append to **Dynamic Lead Sheet** with `Facebook Link`, `Business Name`, and `Lead Activity` = `pending_scrape`.
# smsoutreach
# leaden
# leaden
# leadgen
