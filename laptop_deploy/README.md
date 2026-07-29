# Lead Gen — Scrape Laptop

Runs **scrapesheet** processing for Step 3. Your main PC runs Steps 1–2 and enqueues leads.

## scrapesheet layout (row 2 only)

| link | data |
|------|------|
| Facebook URL (Python fills) | Scrape text (MMM pastes) |

## Setup

1. Install Python 3.10+ (tick "Add to PATH")
2. Copy this folder to the laptop
3. Put service account JSON in `credentials\`
4. Double-click **`install.bat`**
5. Double-click **`run_worker.bat`** (keep open)

## Mini Mouse Macro

Open tab **scrapesheet**. Loop forever on **row 2**:

1. If **link** is not empty and **data** is empty → scrape the page
2. Paste into **data**
3. Repeat

Python clears row 2 after verifying data, then loads the next link.

## Flow

```
Main PC Step 3 → link in scrapesheet row 2
MMM → pastes data
Worker → copies data to Dynamic Lead Sheet, clears row 2, loads next link
```
