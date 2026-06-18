# Cold Email Automation

## Lead source: Google Maps Scraper (`scrappers/google-maps-scraper/`)

A native Go build of [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) — extracts business leads (name, address, phone, website, **email**, ratings, etc.) from Google Maps. Use it to generate prospect lists for outreach. No Docker needed; it's a self-contained binary.

### Build / toolchain notes
- Binary: `scrappers/google-maps-scraper/google-maps-scraper` (~71 MB, prebuilt).
- Requires **Go ≥ 1.26.3** (the repo's `go.mod` hard-requires it). Apt only ships 1.26.0, so the official tarball is installed at `/usr/local/go`. Build with `/usr/local/go/bin/go` on `PATH`.
- **Gotcha:** do not rely on `GOTOOLCHAIN=auto` — its auto-download of the 1.26.3 toolchain stalls here. Use the system Go 1.26.3 with `GOTOOLCHAIN=local`.
- Rebuild if needed:
  ```bash
  cd scrappers/google-maps-scraper
  export PATH=/usr/local/go/bin:$PATH
  go build -o google-maps-scraper .
  ```

### How to run it
All commands run from inside `scrappers/google-maps-scraper/`.

```bash
# 1. Put one search query per line in an input file
echo "dentist in Berlin" > queries.txt

# 2a. Basic scrape → CSV
./google-maps-scraper -input queries.txt -results results.csv -depth 5

# 2b. Cold-email use: extract emails + JSON output (crawls each business site for emails)
./google-maps-scraper -input queries.txt -results out.json -json -email -depth 10

# 2c. Web UI on http://localhost:8080
./google-maps-scraper -data-folder webdata
```

### Key flags
- `-input <file>`   — query file, one search per line
- `-results <file>` — output path (CSV by default)
- `-json`           — JSON output instead of CSV
- `-email`          — also crawl each business website to extract emails (needed for outreach)
- `-depth <n>`      — scroll depth in results (higher = more leads, slower; default 10)
- `-c <n>`          — concurrency (default: half of CPU cores)
- `-lang <code>`    — results language, e.g. `en`
- `-geo "lat,lng"`  — bias search to coordinates
- `-fast-mode`      — fewer fields, faster
- `-exit-on-inactivity 5m` — auto-stop when idle
- Full list: `./google-maps-scraper -h`

### Dependencies / caveats
- Uses **headless Chromium via Playwright**. This host is **Ubuntu 26.04**, unsupported by Playwright — you MUST export `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64` before running, or it refuses to download the browser. The 24.04 fallback build works. Browser is cached at `~/.cache/ms-playwright/` after first download; system libs (libnss3/libatk/libgbm/…) are already installed.
- A `/scrape` skill (`.claude/skills/scrape/`) wraps all of this — e.g. `/scrape 100 restaurants in Berlin`.
- Scraping Google Maps is against Google's ToS; scraped emails are cold contacts — respect GDPR/CAN-SPAM when using output for outreach.
