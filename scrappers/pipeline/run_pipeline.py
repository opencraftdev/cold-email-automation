#!/usr/bin/env python3
"""
run_pipeline.py — one-shot Scrape -> Ingest -> Validate -> Discord pipeline
for the OpenCraft cold-email-automation project.

A single command does the whole flow the /scrape and /validate skills do by hand:

  1. SCRAPE   — runs the local google-maps-scraper binary for your queries.
  2. INGEST   — maps results to the `scraper_leads` schema and inserts them into
                Supabase (deduped against existing rows; no duplicates).
  3. VALIDATE — for each new lead: checks website reachability, matches the
                lead's market to an OpenCraft showcase project (from the brand
                knowledge graph), and writes back validation_status,
                validation_notes, marketing_angle, and a Bahasa-Indonesia
                outreach_message that includes the project's active live link.
  4. NOTIFY   — posts a run summary to a Discord webhook.

All config comes from a .env file next to this script (see .env.example).

Example — one command, end to end:

  python run_pipeline.py --category korean-market --location Bandung --limit 50 \
      "korean market in Bandung" "toko korea Bandung"

Notes:
  * Validation here is the automated/heuristic version (reachability + showcase
    match + templated message). The interactive /validate skill does deeper,
    AI + real-browser verification and more personalised copy.
  * Talks to Supabase over the PostgREST API with the service-role key, so it
    bypasses RLS (server-side use only — never ship the service key to a client).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'requests'. Run: pip install -r requirements.txt")

# python-dotenv is optional but recommended; fall back to os.environ if absent.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


HERE = Path(__file__).resolve().parent
DEFAULT_BIN = (HERE.parent / "google-maps-scraper" / "google-maps-scraper").resolve()

VALID_CATEGORIES = {
    "kecantikan", "wisata", "otomotif", "akomodasi", "kesehatan", "korean-market",
}

# Hosts that are social / link-in-bio, not a real owned website.
SOCIAL_HOSTS = (
    "instagram.com", "facebook.com", "fb.com", "tiktok.com", "linktr.ee",
    "taplink.", "lynk.id", "linkin.bio", "beacons.ai", "twitter.com", "x.com",
    "wa.me", "whatsapp.com", "youtube.com", "shopee.", "tokopedia.",
)

# Per-segment pitch text (short project name + one-line blurb). The actual live
# link is read from the brand knowledge graph at runtime (source of truth), so
# this only supplies the human copy. kesehatan has no showcase -> generic pitch.
SEGMENT_PITCH = {
    "korean-market": (
        "Kiyoo",
        "website pre-order di mana pelanggan pilih produk & varian, lalu ordernya "
        "otomatis masuk ke WhatsApp admin",
    ),
    "kecantikan": (
        "VERDA",
        "landing page premium dengan hero sinematik, brand story, dan koleksi "
        "produk buat bangun trust & konversi",
    ),
    "otomotif": (
        "VELOCE Motors",
        "landing page performa tinggi dengan galeri model, spesifikasi, dan "
        "booking test-drive",
    ),
    "wisata": (
        "Nusantara",
        "landing page wisata bergaya editorial dengan galeri destinasi yang bikin "
        "orang pengen eksplor",
    ),
    "akomodasi": (
        "Aruna Hotel & Resort",
        "landing page hotel dengan daftar kamar + harga, fasilitas, galeri, dan "
        "form booking",
    ),
}


# ----------------------------------------------------------------------------- config

class Config:
    def __init__(self) -> None:
        load_dotenv(HERE / ".env")
        self.supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL") or ""
        self.brand_slug = os.getenv("BRAND_SLUG", "opencraft")
        self.scraper_bin = Path(os.getenv("SCRAPER_BIN", str(DEFAULT_BIN)))
        self.pw_override = os.getenv("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "ubuntu24.04-x64")

    def require_supabase(self) -> None:
        missing = [k for k, v in (
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_SERVICE_ROLE_KEY", self.service_key),
        ) if not v]
        if missing:
            sys.exit(f"Missing required env var(s): {', '.join(missing)} (see .env.example)")

    @property
    def rest(self) -> str:
        return f"{self.supabase_url}/rest/v1"

    @property
    def headers(self) -> dict:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }


# ----------------------------------------------------------------------------- logging

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------- scrape

def depth_for(limit: int) -> int:
    if limit <= 20:
        return 10
    if limit <= 60:
        return 15
    return 20


def run_scraper(cfg: Config, queries: list[str], depth: int, inactivity: str) -> list[dict]:
    """Run the google-maps-scraper binary and return parsed JSON-lines results."""
    if not cfg.scraper_bin.exists():
        sys.exit(f"Scraper binary not found at {cfg.scraper_bin} (set SCRAPER_BIN in .env)")

    workdir = cfg.scraper_bin.parent
    with tempfile.TemporaryDirectory() as tmp:
        qfile = Path(tmp) / "queries.txt"
        out = Path(tmp) / "leads_raw.json"
        qfile.write_text("\n".join(queries) + "\n", encoding="utf-8")

        env = dict(os.environ)
        env["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = cfg.pw_override
        env["PATH"] = f"/usr/local/go/bin:{env.get('PATH', '')}"

        cmd = [
            str(cfg.scraper_bin),
            "-input", str(qfile),
            "-results", str(out),
            "-json", "-email",
            "-depth", str(depth),
            "-exit-on-inactivity", inactivity,
        ]
        log(f"Scraping {len(queries)} query(ies) at depth {depth} ...")
        proc = subprocess.run(cmd, cwd=str(workdir), env=env)
        if proc.returncode != 0:
            log(f"Scraper exited with code {proc.returncode} (continuing with whatever it wrote)")

        if not out.exists():
            return []
        rows = []
        for line in out.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def _num(v):
    if v in (None, "", 0, "0"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_lead(raw: dict, category: str, location: str, query: str) -> dict:
    """Map a scraper JSON entry to a scraper_leads row (field names per gmaps/entry.go)."""
    emails = raw.get("emails") or []
    return {
        "category": category,
        "business_name": (raw.get("title") or "").strip(),
        "phone": (raw.get("phone") or "").strip() or None,
        "website": (raw.get("web_site") or "").strip() or None,
        "email": (emails[0].strip() if emails and emails[0] else None),
        "address": (raw.get("address") or "").strip() or None,
        "rating": _num(raw.get("review_rating")),
        "reviews": (int(raw["review_count"]) if raw.get("review_count") else None),
        "latitude": _num(raw.get("latitude")),
        "longitude": _num(raw.get("longtitude")),  # [sic] scraper field is misspelled
        "maps_url": (raw.get("link") or "").strip() or None,
        "query": query,
        "location": location,
    }


# ----------------------------------------------------------------------------- supabase

def dedupe_key(category: str, name: str, address) -> tuple:
    return (category, (name or "").strip().lower(), (address or "").strip())


def fetch_existing_keys(cfg: Config) -> set:
    r = requests.get(
        f"{cfg.rest}/scraper_leads",
        headers=cfg.headers,
        params={"select": "category,business_name,address"},
        timeout=30,
    )
    r.raise_for_status()
    return {dedupe_key(row["category"], row["business_name"], row.get("address")) for row in r.json()}


def ingest(cfg: Config, leads: list[dict]) -> list[dict]:
    """Insert new (deduped) leads; return the inserted rows including their ids."""
    existing = fetch_existing_keys(cfg)
    seen = set(existing)
    new_rows = []
    for lead in leads:
        if not lead["business_name"]:
            continue
        key = dedupe_key(lead["category"], lead["business_name"], lead["address"])
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(lead)

    if not new_rows:
        return []

    headers = {**cfg.headers, "Prefer": "return=representation"}
    inserted = []
    for i in range(0, len(new_rows), 200):  # chunk to keep payloads sane
        chunk = new_rows[i:i + 200]
        r = requests.post(f"{cfg.rest}/scraper_leads", headers=headers, json=chunk, timeout=60)
        r.raise_for_status()
        inserted.extend(r.json())
    return inserted


def load_showcase_map(cfg: Config) -> dict:
    """segment -> {label, live_url} for showcase case studies with an active link."""
    r = requests.get(
        f"{cfg.rest}/brand_knowledge_nodes",
        headers=cfg.headers,
        params={
            "brand_slug": f"eq.{cfg.brand_slug}",
            "type": "eq.studi_kasus",
            "select": "label,props",
        },
        timeout=30,
    )
    r.raise_for_status()
    out = {}
    for node in r.json():
        props = node.get("props") or {}
        seg = props.get("segment")
        url = props.get("live_url")
        if seg and url and props.get("link_active") in (True, "true"):
            out[seg] = {"label": node.get("label", ""), "live_url": url}
    return out


def write_validation(cfg: Config, updates: list[dict]) -> None:
    """Write validation results back, one PATCH per lead (by id). A partial upsert
    can't be done via POST because NOT NULL columns aren't in the payload."""
    if not updates:
        return
    headers = {**cfg.headers, "Prefer": "return=minimal"}
    for u in updates:
        body = {k: v for k, v in u.items() if k != "id"}
        r = requests.patch(
            f"{cfg.rest}/scraper_leads", headers=headers,
            params={"id": f"eq.{u['id']}"}, json=body, timeout=30,
        )
        r.raise_for_status()


# ----------------------------------------------------------------------------- validate

def classify_website(url: str | None) -> str:
    """Return one of: none | live | dead | social | social_dead."""
    if not url:
        return "none"
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    is_social = any(s in host for s in SOCIAL_HOSTS)
    ok = reachable(url)
    if is_social:
        return "social" if ok else "social_dead"
    return "live" if ok else "dead"


def reachable(url: str) -> bool:
    if "//" not in url:
        url = "https://" + url
    ua = {"User-Agent": "Mozilla/5.0 (compatible; OpenCraftLeadBot/1.0)"}
    try:
        resp = requests.get(url, headers=ua, timeout=12, allow_redirects=True)
        return resp.status_code < 400
    except requests.RequestException:
        return False


def build_message(name: str, project: dict | None, segment: str, wcls: str) -> str:
    """Compose a Bahasa-Indonesia outreach message. Sends the project's active
    link when a showcase project matches; otherwise pitches the service, no link."""
    if wcls == "social":
        obs = "kehadiran online-nya masih ngandelin sosial media / link-in-bio dan belum ada website sendiri"
    else:  # none
        obs = "belum punya website sendiri, jadi pemesanan & info masih tersebar manual"

    if project and segment in SEGMENT_PITCH:
        short, blurb = SEGMENT_PITCH[segment]
        return (
            f"Halo Tim {name},\n\n"
            f"Saya dari OpenCraft. Saya lihat {name} {obs}, dan ini mirip dengan yang "
            f"kami kerjakan di {short} — {blurb}.\n\n"
            f"Boleh dilihat langsung contohnya di sini:\n{project['live_url']}\n\n"
            f"Kami rasa {name} bisa dapat manfaat serupa. Boleh saya kirimkan proposal "
            f"singkatnya? Tidak ada kewajiban apa pun.\n\n"
            f"Terima kasih,\nTim OpenCraft"
        )
    # Generic service pitch (no matching showcase project -> no link).
    return (
        f"Halo Tim {name},\n\n"
        f"Saya dari OpenCraft. Saya lihat {name} {obs}. Kami bantu bisnis bikin "
        f"website / landing page sendiri yang menonjolkan produk, lokasi, dan kontak "
        f"biar lebih kredibel dan gampang ditemukan calon pelanggan baru.\n\n"
        f"Kami rasa {name} bisa dapat manfaat dari website seperti itu. Boleh saya "
        f"kirimkan contoh & proposal singkatnya? Tidak ada kewajiban apa pun.\n\n"
        f"Terima kasih,\nTim OpenCraft"
    )


def validate_lead(lead: dict, showcase: dict) -> dict:
    """Decide status + notes + angle + message for one inserted lead row."""
    name = lead["business_name"]
    segment = lead["category"]
    wcls = classify_website(lead.get("website"))
    has_contact = bool(lead.get("phone") or lead.get("maps_url"))
    project = showcase.get(segment)

    base = {
        "id": lead["id"],
        "validation_status": "needs_review",
        "validation_notes": None,
        "marketing_angle": None,
        "outreach_message": None,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    if wcls == "dead":
        base["validation_status"] = "invalid"
        base["validation_notes"] = "domain website mati/parked (tidak bisa diakses)"
        return base
    if wcls == "social_dead":
        base["validation_status"] = "needs_review"
        base["validation_notes"] = "link sosial/link-in-bio tidak bisa diakses saat dicek"
        return base
    if wcls == "live":
        base["validation_status"] = "valid"
        base["validation_notes"] = "website aktif/online — peluang upsell (otomasi/landing baru)"
        base["marketing_angle"] = f"{segment} — sudah punya website aktif; upsell perbaikan/otomasi (layanan OpenCraft)"
        base["outreach_message"] = (
            f"Halo Tim {name},\n\n"
            f"Saya dari OpenCraft. Saya lihat {name} sudah punya website yang aktif. "
            f"Kami bantu bisnis bikin website lebih konversi + otomasi (mis. order/CS "
            f"via WhatsApp). Boleh saya kirimkan contoh & proposal singkatnya? "
            f"Tidak ada kewajiban apa pun.\n\nTerima kasih,\nTim OpenCraft"
        )
        return base
    if wcls in ("social", "none"):
        if wcls == "none" and not has_contact:
            base["validation_status"] = "needs_review"
            base["validation_notes"] = "tanpa website & info kontak minim — perlu cek manual"
            return base
        base["validation_status"] = "valid"
        gap = "aktif di sosmed, belum ada website" if wcls == "social" else "tanpa website (listing aktif)"
        base["validation_notes"] = f"{gap}. segment: {segment}"
        if project and segment in SEGMENT_PITCH:
            short, _ = SEGMENT_PITCH[segment]
            base["marketing_angle"] = (
                f"{segment} — {gap}; tawarkan website ala {short} ({project['live_url']})"
            )
        else:
            base["marketing_angle"] = f"{segment} — {gap}; tawarkan website/landing page (layanan OpenCraft)"
        base["outreach_message"] = build_message(name, project, segment, wcls)
        return base

    return base


# ----------------------------------------------------------------------------- discord

def notify_discord(cfg: Config, summary: dict) -> None:
    if not cfg.discord_webhook:
        log("No DISCORD_WEBHOOK_URL set — skipping Discord notification.")
        return
    counts = summary["counts"]
    fields = [
        {"name": "Queries", "value": "\n".join(f"• {q}" for q in summary["queries"])[:1024] or "—", "inline": False},
        {"name": "Category", "value": summary["category"], "inline": True},
        {"name": "Scraped", "value": str(summary["scraped"]), "inline": True},
        {"name": "New inserted", "value": str(summary["inserted"]), "inline": True},
        {"name": "✅ valid", "value": str(counts.get("valid", 0)), "inline": True},
        {"name": "❌ invalid", "value": str(counts.get("invalid", 0)), "inline": True},
        {"name": "🔎 needs_review", "value": str(counts.get("needs_review", 0)), "inline": True},
        {"name": "📧 with email", "value": str(summary["with_email"]), "inline": True},
        {"name": "🔗 with showcase link", "value": str(summary["with_link"]), "inline": True},
    ]
    embed = {
        "title": "Scrape + Validate selesai",
        "description": f"Pipeline cold-email OpenCraft — {summary['category']} @ {summary['location'] or '—'}",
        "color": 0x2ECC71 if summary["inserted"] else 0xF1C40F,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "cold-email-automation • run_pipeline.py"},
    }
    try:
        r = requests.post(cfg.discord_webhook, json={"embeds": [embed]}, timeout=20)
        r.raise_for_status()
        log("Discord notified.")
    except requests.RequestException as e:
        log(f"Discord notification failed: {e}")


# ----------------------------------------------------------------------------- main

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape -> ingest -> validate -> Discord, in one command.",
    )
    p.add_argument("queries", nargs="*", help='Search queries, e.g. "korean market in Bandung"')
    p.add_argument("--category", required=True, choices=sorted(VALID_CATEGORIES),
                   help="Tracking bucket (must match the scraper_leads check constraint)")
    p.add_argument("--location", default="", help="City/area searched (stored on each lead)")
    p.add_argument("--business-type", default="",
                   help='If no queries given, build "<type> in <location>" from this')
    p.add_argument("--limit", type=int, default=50, help="Max leads to keep from the scrape (default 50)")
    p.add_argument("--depth", type=int, default=0, help="Scraper scroll depth (default: derived from --limit)")
    p.add_argument("--inactivity", default="3m", help="Auto-stop scraper after this idle time (default 3m)")
    p.add_argument("--skip-scrape", action="store_true",
                   help="Skip scraping; just validate existing pending leads")
    p.add_argument("--no-validate", action="store_true", help="Ingest only; do not validate")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    cfg = Config()
    cfg.require_supabase()

    queries = list(args.queries)
    if not queries and args.business_type:
        q = args.business_type
        if args.location:
            q += f" in {args.location}"
        queries = [q]

    # ---- scrape + ingest -------------------------------------------------------
    inserted: list[dict] = []
    scraped = 0
    if not args.skip_scrape:
        if not queries:
            sys.exit("No queries given. Provide search strings or --business-type.")
        depth = args.depth or depth_for(args.limit)
        raw = run_scraper(cfg, queries, depth, args.inactivity)
        raw = raw[: args.limit]
        scraped = len(raw)
        log(f"Scraped {scraped} place(s).")
        mapped = [map_lead(r, args.category, args.location, queries[0]) for r in raw]
        inserted = ingest(cfg, mapped)
        log(f"Ingested {len(inserted)} new lead(s) (rest were duplicates).")
    else:
        log("Skipping scrape (--skip-scrape).")

    # ---- validate --------------------------------------------------------------
    counts = {"valid": 0, "invalid": 0, "needs_review": 0}
    with_link = 0
    targets = inserted
    if args.skip_scrape:
        r = requests.get(
            f"{cfg.rest}/scraper_leads",
            headers=cfg.headers,
            params={"validation_status": "eq.pending", "select": "*"},
            timeout=60,
        )
        r.raise_for_status()
        targets = r.json()

    if args.no_validate:
        log("Skipping validation (--no-validate).")
    elif targets:
        showcase = load_showcase_map(cfg)
        log(f"Validating {len(targets)} lead(s) against {len(showcase)} showcase segment(s) ...")
        updates = []
        for lead in targets:
            res = validate_lead(lead, showcase)
            updates.append(res)
            counts[res["validation_status"]] = counts.get(res["validation_status"], 0) + 1
            if res.get("outreach_message") and "https://" in (res["outreach_message"] or ""):
                with_link += 1
        write_validation(cfg, updates)
        log(f"Validation written: {counts}")

    with_email = sum(1 for l in inserted if l.get("email"))

    # ---- notify ----------------------------------------------------------------
    summary = {
        "queries": queries or ["(validate-only)"],
        "category": args.category,
        "location": args.location,
        "scraped": scraped,
        "inserted": len(inserted) if not args.skip_scrape else len(targets),
        "with_email": with_email,
        "with_link": with_link,
        "counts": counts,
    }
    notify_discord(cfg, summary)
    log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
