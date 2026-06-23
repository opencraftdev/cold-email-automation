#!/usr/bin/env python3
"""notify_rollup.py — post ONE consolidated Discord summary for a full nightly run.

Reads the per-category summary JSON files written by `run_pipeline.py --summary-out`
and posts a single monospace-table message to DISCORD_WEBHOOK_URL. This is what lets
run_nightly.sh notify once at the end instead of once per category.

Usage:
  python notify_rollup.py --summary-dir <dir> [--duration <seconds>] \
      [--location Bandung] [--target 5] [--expect korean-market,kecantikan,...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'requests'. Run: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):
        return False

HERE = Path(__file__).resolve().parent

# Canonical display order for the table rows.
CATEGORY_ORDER = [
    "korean-market", "kecantikan", "otomotif", "wisata", "akomodasi", "kesehatan",
]

# Aligned-column layout (no emojis inside cells so columns line up on every device).
ROW_FMT = "{:<14}{:>6}{:>7}{:>6}{:>6}"


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def load_summaries(summary_dir: Path) -> dict:
    """category -> summary dict, for every *.json in the dir that parses."""
    out = {}
    for p in sorted(summary_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cat = data.get("category")
        if cat:
            out[cat] = data
    return out


def ordered_categories(summaries: dict, expect: list[str]) -> list[str]:
    if expect:
        return expect
    present = [c for c in CATEGORY_ORDER if c in summaries]
    extra = [c for c in summaries if c not in CATEGORY_ORDER]
    return present + extra


def build_message(summaries: dict, cats: list[str], location: str,
                  target: int, duration: int) -> tuple[str, str]:
    """Return (status_emoji, message_content)."""
    header = ROW_FMT.format("Kategori", "Baru", "Valid", "Rev", "Inv")
    divider = "-" * len(header)
    body_rows = []
    tot = [0, 0, 0, 0]  # baru, valid, rev, inv
    tot_email = tot_link = errors = 0

    for cat in cats:
        s = summaries.get(cat)
        if not s:
            body_rows.append(ROW_FMT.format(cat[:14], "ERR", "-", "-", "-"))
            errors += 1
            continue
        c = s.get("counts", {}) or {}
        baru = int(s.get("inserted", 0) or 0)
        v = int(c.get("valid", 0) or 0)
        rev = int(c.get("needs_review", 0) or 0)
        inv = int(c.get("invalid", 0) or 0)
        body_rows.append(ROW_FMT.format(cat[:14], baru, v, rev, inv))
        tot[0] += baru; tot[1] += v; tot[2] += rev; tot[3] += inv
        tot_email += int(s.get("with_email", 0) or 0)
        tot_link += int(s.get("with_link", 0) or 0)

    total = ROW_FMT.format("TOTAL", *tot)
    table = "\n".join([header, divider, *body_rows, divider, total])

    status = "🔴" if errors else ("🟢" if tot[0] > 0 else "🟡")
    # Date in WIB (host cron runs in UTC).
    wib = datetime.now(timezone.utc) + timedelta(hours=7)
    date_str = wib.strftime("%d %b %Y")

    content = (
        f"# {status} Cold-Email Pipeline · Laporan Harian\n"
        f"{len(cats)} kategori @ **{location or '-'}** · "
        f"target **{target} lead/kategori** · 20:00 WIB\n\n"
        f"```\n{table}\n```\n"
        f"📧 **{tot_email}** punya email · 🔗 **{tot_link}** dapat showcase link · "
        f"⏱️ durasi **{fmt_duration(duration)}**"
        + (f" · ⚠️ **{errors}** kategori gagal" if errors else "")
        + f"\n`cold-email-automation · run_nightly.sh` · {date_str}"
    )
    return status, content


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post one consolidated Discord rollup.")
    p.add_argument("--summary-dir", required=True, help="Dir of per-category summary JSON files")
    p.add_argument("--duration", type=int, default=0, help="Total run duration in seconds")
    p.add_argument("--location", default="", help="Location label for the header")
    p.add_argument("--target", type=int, default=5, help="Per-category lead target (for the header)")
    p.add_argument("--expect", default="",
                   help="Comma-separated categories that SHOULD be present; missing ones show ERR")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    load_dotenv(HERE / ".env")
    webhook = os.getenv("DISCORD_WEBHOOK_URL") or ""
    args = parse_args(argv)

    summaries = load_summaries(Path(args.summary_dir))
    expect = [c.strip() for c in args.expect.split(",") if c.strip()]
    cats = ordered_categories(summaries, expect)
    if not cats:
        print("No summaries found — nothing to notify.", file=sys.stderr)
        return 0

    _, content = build_message(summaries, cats, args.location, args.target, args.duration)

    if not webhook:
        print("No DISCORD_WEBHOOK_URL set — printing rollup instead:\n")
        print(content)
        return 0
    try:
        r = requests.post(webhook, json={"content": content}, timeout=20)
        r.raise_for_status()
        print(f"Rollup posted to Discord ({r.status_code}).")
    except requests.RequestException as e:
        print(f"Rollup post failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
