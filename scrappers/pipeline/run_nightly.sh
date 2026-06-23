#!/usr/bin/env bash
# Nightly cold-email lead run: scrape -> ingest -> validate, once per category,
# 5 leads each, in Bandung — then ONE consolidated Discord summary at the end.
# Scheduled at 20:00 WIB (13:00 UTC). See crontab.
#
# Notes:
#  * Each category runs with --no-notify and writes a JSON summary; after all
#    categories finish, notify_rollup.py posts a single table message. So the
#    nightly run notifies once, not six times.
#  * Sources .env into the environment so the pipeline works even though
#    python-dotenv isn't installed on this host (the script's load_dotenv
#    falls back to a no-op and just reads os.environ).
#  * Uses system python3 (has `requests`); no venv needed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

LOG="$HERE/nightly.log"
exec >>"$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
echo "================ nightly run start $(ts) ================"

# --- load env -------------------------------------------------------------
if [[ ! -f .env ]]; then
  echo "[FATAL] .env missing"; exit 1
fi
set -a; . ./.env; set +a

if [[ "${SUPABASE_SERVICE_ROLE_KEY:-}" == "PASTE_SERVICE_ROLE_KEY_HERE" || -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]]; then
  echo "[FATAL] SUPABASE_SERVICE_ROLE_KEY not set (still a placeholder). Aborting."; exit 1
fi

# --- categories: "category|business-type" (location fixed to Bandung) -----
LOCATION="Bandung"
LIMIT=5
JOBS=(
  "korean-market|korean market"
  "kecantikan|klinik kecantikan"
  "otomotif|bengkel mobil"
  "wisata|tempat wisata"
  "akomodasi|hotel"
  "kesehatan|klinik"
)

# Per-category summaries collect here; one rollup is posted after the loop.
SUMDIR="$(mktemp -d)"
trap 'rm -rf "$SUMDIR"' EXIT
START=$(date +%s)

rc_total=0
expect=""
for job in "${JOBS[@]}"; do
  cat="${job%%|*}"
  btype="${job#*|}"
  expect="${expect:+$expect,}$cat"
  echo "---- [$(ts)] category=$cat type='$btype' loc=$LOCATION limit=$LIMIT ----"
  python3 run_pipeline.py \
    --category "$cat" \
    --location "$LOCATION" \
    --business-type "$btype" \
    --limit "$LIMIT" \
    --no-notify \
    --summary-out "$SUMDIR/$cat.json"
  rc=$?
  echo "---- [$(ts)] category=$cat finished rc=$rc ----"
  [[ $rc -ne 0 ]] && rc_total=$rc
done

# --- one consolidated Discord summary -------------------------------------
DURATION=$(( $(date +%s) - START ))
echo "---- [$(ts)] posting consolidated Discord rollup (duration ${DURATION}s) ----"
python3 notify_rollup.py \
  --summary-dir "$SUMDIR" \
  --duration "$DURATION" \
  --location "$LOCATION" \
  --target "$LIMIT" \
  --expect "$expect"

echo "================ nightly run done $(ts) rc=$rc_total ================"
exit $rc_total
