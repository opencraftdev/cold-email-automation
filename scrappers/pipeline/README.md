# Lead pipeline — scrape → ingest → validate → Discord (one script)

`run_pipeline.py` runs the whole cold-email lead flow end-to-end in a single command:

1. **Scrape** — runs the local `google-maps-scraper` binary for your queries.
2. **Ingest** — maps results to the `scraper_leads` schema and inserts them into
   Supabase (deduped against existing rows — no duplicates).
3. **Validate** — for each new lead: checks website reachability, matches the
   lead's market to an OpenCraft showcase project (from the brand knowledge
   graph), and writes back `validation_status`, `validation_notes`,
   `marketing_angle`, and a Bahasa-Indonesia `outreach_message` that includes the
   matched project's **active live link**.
4. **Notify** — posts a run summary to a Discord webhook.

## Setup (once)

```bash
cd scrappers/pipeline
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env          # then fill in the values (see below)
```

`.env` keys:

| key | what |
|---|---|
| `SUPABASE_URL` | `https://wdzmuniyqqyngzckeoph.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service-role key (Supabase → Project Settings → API). Bypasses RLS — keep secret. |
| `DISCORD_WEBHOOK_URL` | webhook to notify when the run finishes |
| `BRAND_SLUG` | `opencraft` (default) |
| `SCRAPER_BIN` | optional override for the scraper binary path |
| `PLAYWRIGHT_HOST_PLATFORM_OVERRIDE` | `ubuntu24.04-x64` (required on this host) |

`.env` is gitignored — secrets never get committed.

## Run

```bash
# Scrape Korean-market leads in Bandung, ingest, validate, notify Discord:
python run_pipeline.py --category korean-market --location Bandung --limit 50 \
    "korean market in Bandung" "toko korea Bandung"

# Build the query from a business type instead of passing it literally:
python run_pipeline.py --category kecantikan --location Jakarta --limit 30 \
    --business-type "klinik kecantikan"

# Validate-only (no scrape) — re-process whatever is still pending:
python run_pipeline.py --category korean-market --skip-scrape
```

### Options

| flag | default | meaning |
|---|---|---|
| `queries...` | — | one or more Google Maps search strings |
| `--category` | (required) | one of `kecantikan, wisata, otomotif, akomodasi, kesehatan, korean-market` |
| `--location` | "" | city/area, stored on each lead |
| `--business-type` | "" | builds `"<type> in <location>"` when no queries are given |
| `--limit` | 50 | max leads to keep from the scrape |
| `--depth` | derived | scraper scroll depth (10/15/20 by limit) |
| `--inactivity` | 3m | auto-stop the scraper after this idle time |
| `--skip-scrape` | off | skip scraping; just validate existing `pending` leads |
| `--no-validate` | off | ingest only; don't validate |

## How validation works here

This is the **automated** validation: website reachability (live / dead / social-only),
plus a market→showcase match and a templated message. The interactive **`/validate`**
skill does deeper AI + real-browser verification and more personalised copy — use it
when you want the higher-touch pass. Statuses written:

- `valid` — reachable website (upsell), or social-only/no-website with a real
  listing (the prime outreach targets; these get the showcase link).
- `invalid` — dead/parked website domain.
- `needs_review` — social link down, or no website and no contact info to confirm.

Showcase links are only sent for segments that have an active case study in the
brand knowledge graph (`korean-market→Kiyoo`, `kecantikan→VERDA`,
`otomotif→VELOCE`, `wisata→Nusantara`, `akomodasi→Aruna`). `kesehatan` and any
unmatched segment fall back to a generic service pitch with no link.
