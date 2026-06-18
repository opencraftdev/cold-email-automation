---
name: scrape
description: Scrape business leads from Google Maps (name, address, phone, website, email, rating) using the local google-maps-scraper binary. Use when the user types /scrape followed by a count and a business type, e.g. "/scrape 100 restaurant data", "/scrape 50 dentists in Berlin", "/scrape coffee shops in Austin TX". Produces a JSON + CSV lead list for cold-email outreach.
---

# /scrape — Google Maps lead scraper

Wraps the prebuilt binary at `scrappers/google-maps-scraper/google-maps-scraper` to pull business leads from Google Maps.

## Step 1 — Parse the request

From the user's arguments, extract three things:

- **count** — how many leads they want (integer). Default `50` if none given.
- **business type** — the thing to search for (e.g. `restaurant`, `dentists`, `coffee shop`, `plumber`). Strip filler words like "data", "leads", "scrape".
- **location** — city / area (e.g. `Berlin`, `Austin TX`). May be missing.

Examples:
- `/scrape 100 restaurant data` → count=100, type=`restaurant`, location=(none)
- `/scrape 50 dentists in Berlin` → count=50, type=`dentists`, location=`Berlin`
- `/scrape coffee shops in Austin TX` → count=50, type=`coffee shops`, location=`Austin TX`

**If location is missing, ask the user for a city/area before scraping** (Google Maps results are location-bound; without one the data is near-useless). Use AskUserQuestion with 2-3 sensible options plus their own input. Do not invent a location silently.

## Step 2 — Build the query file

The search query is `"<business type> in <location>"`. Write it to a queries file (one query per line). For a single type+location that's one line.

```bash
cd scrappers/google-maps-scraper
mkdir -p output
echo "<business type> in <location>" > output/queries.txt
```

## Step 3 — Pick depth and run

Google Maps returns up to ~120 places per query. `-depth` controls scroll depth (more depth = more results, slower). Map count → depth generously, then trim to the exact count afterward:

- count ≤ 20 → `-depth 10`
- count ≤ 60 → `-depth 15`
- count ≤ 120 → `-depth 20`
- count > 120 → `-depth 20` and warn the user that a single query/location caps near ~120; suggest splitting across multiple cities or sub-areas (add more lines to queries.txt).

Run with email extraction and JSON output:

```bash
cd scrappers/google-maps-scraper
export PATH=/usr/local/go/bin:$PATH
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64   # REQUIRED on Ubuntu 26.04 (see caveats)
./google-maps-scraper \
  -input output/queries.txt \
  -results output/leads_raw.json \
  -json -email \
  -depth <DEPTH> \
  -exit-on-inactivity 3m
```

Notes:
- `-email` crawls each business website for an email address — slower but essential for cold outreach.
- `-exit-on-inactivity 3m` stops the run automatically when it goes idle, so it never hangs.
- Run this in the background (`run_in_background: true`) since it can take several minutes, and poll the output file for progress.

## Step 4 — Trim to the requested count + make a CSV

The scraper writes one JSON object per line. Take the first N and also produce a CSV the user can open:

```bash
cd scrappers/google-maps-scraper/output
# trim to N
head -n <COUNT> leads_raw.json > leads.json
# quick CSV (name, phone, website, email, address, rating)
jq -r '[.title, .phone, .web_site, (.emails[0]? // ""), .address, (.review_rating|tostring)] | @csv' leads.json \
  | sed '1i "name","phone","website","email","address","rating"' > leads.csv
echo "rows: $(wc -l < leads.json)"
# how many have an email
echo "with email: $(jq -r 'select((.emails|length)>0) | .title' leads.json | wc -l)"
```

The scraper's JSON field names (verified against `gmaps/entry.go`): `title`, `phone`, `web_site`, `emails` (array), `address`, `review_rating`, `category`, `latitude`, `longtitude`, `link`.

## Step 5 — Report

Tell the user:
- How many leads were actually captured (may be fewer than asked if Maps had fewer listings).
- How many have an email (the useful subset for cold email).
- The output paths: `scrappers/google-maps-scraper/output/leads.json` and `leads.csv`.
- Show a small sample (first 3 businesses) as a markdown table.

## Caveats
- The binary drives **headless Chromium via Playwright**. This host is **Ubuntu 26.04**, which Playwright doesn't officially support — it will refuse to download Chromium unless `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64` is set (already included in the run command above). The 24.04 fallback browser build runs fine. The browser (~165 MB) downloads once on first run, then is cached at `~/.cache/ms-playwright/`. The required system libs (libnss3, libatk, libgbm, etc.) are already installed.
- Niche queries in one city often return far fewer than the requested count (Maps only has what exists). Report the real number; don't pad. Many small/local businesses have no website or email — phone is the most reliable field.
- Scraping Google Maps violates Google's ToS; scraped emails are cold contacts — respect GDPR/CAN-SPAM in any outreach.
