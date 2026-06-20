---
name: scrape
description: Scrape business leads from Google Maps (name, address, phone, website, email, rating) using the local google-maps-scraper binary, then push them into the OpenCraft Supabase database for tracking. Use when the user types /scrape followed by a count and a business type, e.g. "/scrape 100 klinik kecantikan in Jakarta", "/scrape 50 bengkel mobil in Surabaya", "/scrape hotel in Bali". Produces a JSON + CSV lead list AND inserts the leads into the `scraper_leads` table (visible in the dashboard's Scrapers menu).
---

# /scrape — Google Maps lead scraper → OpenCraft database

Wraps the prebuilt binary at `scrappers/google-maps-scraper/google-maps-scraper` to pull business leads from Google Maps, then pushes them into the OpenCraft Supabase project (`central-apps`, ref **`wdzmuniyqqyngzckeoph`**) via the Supabase MCP so they show up in the dashboard's **Scrapers** menu.

## Step 1 — Parse the request

From the user's arguments, extract four things:

- **count** — how many leads they want (integer). Default `50` if none given.
- **business type** — the thing to search for (e.g. `klinik kecantikan`, `bengkel mobil`, `hotel`, `klinik gigi`). Strip filler words like "data", "leads", "scrape".
- **location** — city / area (e.g. `Jakarta`, `Surabaya`, `Bali`). May be missing.
- **category** — which of the **6 tracking buckets** this scrape belongs to. REQUIRED for the database push. The six category slugs and what they cover:

  | slug | label | typical business types |
  |---|---|---|
  | `kecantikan` | Kecantikan | klinik kecantikan, salon, skincare, spa, barbershop, nail art |
  | `wisata` | Wisata | tempat wisata, tour operator, travel agent, objek wisata, taman |
  | `otomotif` | Otomotif | bengkel mobil/motor, dealer, cuci mobil, sparepart, rental mobil |
  | `akomodasi` | Akomodasi | hotel, villa, guest house, homestay, kos, penginapan |
  | `kesehatan` | Kesehatan | klinik, apotek, rumah sakit, dokter, lab, fisioterapi |
  | `korean-market` | Korean Market | korean mart, toko korea, korean grocery, k-mart, korean food store |

  **Infer the category from the business type** using the table above. If it's genuinely ambiguous, ask the user with AskUserQuestion offering the 6 categories. The category slug must be exactly one of the six above — the DB rejects anything else.

Examples:
- `/scrape 100 klinik kecantikan in Jakarta` → count=100, type=`klinik kecantikan`, location=`Jakarta`, category=`kecantikan`
- `/scrape 50 bengkel mobil in Surabaya` → count=50, type=`bengkel mobil`, location=`Surabaya`, category=`otomotif`
- `/scrape hotel in Bali` → count=50, type=`hotel`, location=`Bali`, category=`akomodasi`

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

The scraper's JSON field names (verified against `gmaps/entry.go`): `title`, `phone`, `web_site`, `emails` (array), `address`, `review_rating`, `review_count`, `category`, `latitude`, `longtitude`, `link`.

## Step 5 — Push the leads into the OpenCraft database

This is the important new step: every scrape lands in the `scraper_leads` table of the `central-apps` Supabase project (ref **`wdzmuniyqqyngzckeoph`**), so the dashboard's **Scrapers** menu shows it. Build a single `INSERT … ON CONFLICT` statement from `leads.json` and run it via the Supabase MCP.

**5a — Generate the SQL** (from `scrappers/google-maps-scraper/output`). Set the three context variables to the values parsed in Step 1:

```bash
cd scrappers/google-maps-scraper/output
CATEGORY="kecantikan"                          # one of the 6 slugs — MUST match the DB check constraint
LOCATION="Jakarta"                             # city/area searched
QUERY="klinik kecantikan in Jakarta"           # the exact query line used

# jq filter → one SQL VALUES tuple per lead, with SQL-safe escaping.
cat > to_sql.jq <<'JQ'
def s: if . == null or . == "" then "null" else "'" + (tostring | gsub("'";"''")) + "'" end;
def n: if . == null then "null" else tostring end;
"(" + ($cat|s) + "," + (.title|s) + "," + (.phone|s) + "," + (.web_site|s) + ","
    + ((.emails[0] // null)|s) + "," + (.address|s) + ","
    + ((.review_rating // null)|n) + "," + ((.review_count // null)|n) + ","
    + ((.latitude // null)|n) + "," + ((.longtitude // null)|n) + ","
    + (.link|s) + "," + ($q|s) + "," + ($loc|s) + ")"
JQ

VALUES=$(jq -r --arg cat "$CATEGORY" --arg loc "$LOCATION" --arg q "$QUERY" -f to_sql.jq leads.json | paste -sd, -)

cat > insert.sql <<SQL
insert into scraper_leads
  (category, business_name, phone, website, email, address, rating, reviews, latitude, longitude, maps_url, query, location)
values
$VALUES
on conflict (category, lower(business_name), coalesce(address, '')) do update set
  phone      = excluded.phone,
  website    = excluded.website,
  email      = coalesce(excluded.email, scraper_leads.email),
  rating     = excluded.rating,
  reviews    = excluded.reviews,
  maps_url   = excluded.maps_url,
  query      = excluded.query,
  location   = excluded.location,
  scraped_at = now();
SQL
echo "SQL bytes: $(wc -c < insert.sql)"
```

**5b — Run it via the Supabase MCP.** Read `insert.sql` and pass its contents to `mcp__supabase__execute_sql` with `project_id: "wdzmuniyqqyngzckeoph"`. The MCP uses the service-role key, so it bypasses RLS and writes directly. `ON CONFLICT` makes re-scrapes idempotent — the same business refreshes in place instead of duplicating.

Notes:
- The `rating` column is `numeric(2,1)` (0.0–5.0). The scraper's `review_rating` of `0` for unrated places is stored as-is.
- If `leads.json` is large, the single statement can be big but well within limits; only split into batches of ~500 tuples if `execute_sql` ever rejects the size.
- Confirm the write by checking the returned row count, or run a quick `select count(*) from scraper_leads where category = '<CATEGORY>'`.

## Step 6 — Report

Tell the user:
- How many leads were actually captured (may be fewer than asked if Maps had fewer listings).
- How many have an email (the useful subset for cold email).
- **How many rows were pushed to the database** and the category they landed under — remind them they're now visible in the dashboard's **Scrapers** menu.
- The output paths: `scrappers/google-maps-scraper/output/leads.json` and `leads.csv`.
- Show a small sample (first 3 businesses) as a markdown table.

## Caveats
- The binary drives **headless Chromium via Playwright**. This host is **Ubuntu 26.04**, which Playwright doesn't officially support — it will refuse to download Chromium unless `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64` is set (already included in the run command above). The 24.04 fallback browser build runs fine. The browser (~165 MB) downloads once on first run, then is cached at `~/.cache/ms-playwright/`. The required system libs (libnss3, libatk, libgbm, etc.) are already installed.
- Niche queries in one city often return far fewer than the requested count (Maps only has what exists). Report the real number; don't pad. Many small/local businesses have no website or email — phone is the most reliable field.
- Scraping Google Maps violates Google's ToS; scraped emails are cold contacts — respect GDPR/CAN-SPAM in any outreach.
