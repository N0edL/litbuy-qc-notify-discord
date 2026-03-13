# Litbuy qc notify discord

A small Playwright-based notifier that watches your Litbuy warehouse, detects new items with QC photos, sends Discord webhook embeds, and stores processed data in SQLite.

## Features

- Reuses Playwright signed-in state from state.json
- Auto-login fallback when session expires
- Scrapes order rows from the warehouse page
- Extracts QC image URLs (including lazy-loaded images)
- Sends Discord embeds for new orders with QC photos
- Skips items without QC photos
- Stores processed orders and QC URLs in warehouse_qc.db

## Requirements

- Python 3.10+
- Playwright
- python-dotenv

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install playwright python-dotenv
playwright install
```

3. Create a .env file in the project root:

```env
EMAIL=your_litbuy_email
PASSWORD=your_litbuy_password
DISCORD_WEBHOOK_URL=your_discord_webhook_url
```

## Run

```bash
python fetch.py
```

## Data Files

- state.json: saved authenticated browser state
- warehouse_qc.db: SQLite database for processed orders and QC URLs

## Notes

- If state.json is valid, login is skipped.
- If state.json is expired, the script logs in again and rewrites state.json.
- Only new orders with QC photos are notified.
