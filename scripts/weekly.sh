#!/usr/bin/env bash
# Weekly loop: select universes, run the desks, ingest verdicts, print the
# candidates and the scorecard. Candidates are for review — not orders.
#
# Costs nothing in a week with no new filings: every verdict is a cache hit.
# The two universes are re-selected each week from current Finviz values.
set -euo pipefail

# The checkout: AIHF_REPO if set (the ~/.hedge-fund wrapper sets it), else this
# script's own parent directory when run from the repo.
REPO=${AIHF_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
HOME_DIR="$HOME/.hedge-fund"
DATE=$(date +%F)
LOG="$HOME_DIR/logs/weekly-$DATE.log"
RECORDS="$HOME_DIR/records"
mkdir -p "$HOME_DIR/logs" "$RECORDS"
exec > >(tee -a "$LOG") 2>&1

cd "$REPO"
echo "== weekly $DATE =="

# 1. Universes. A failed, empty, or throttled screen must never reach aihf.
QUALITY=$(poetry run aihf-universe quality --limit 10)
VALUE=$(poetry run aihf-universe value --limit 10)
[ -n "$QUALITY" ] || { echo "quality universe is empty; stopping" >&2; exit 1; }
[ -n "$VALUE" ] || { echo "value universe is empty; stopping" >&2; exit 1; }
UNION=$( (tr ',' '\n' <<<"$QUALITY"; tr ',' '\n' <<<"$VALUE") | awk 'NF && !seen[$0]++' | paste -sd, -)
echo "quality: $QUALITY"
echo "value:   $VALUE"
echo "union:   $UNION"

# 2. Desks, all at low effort. INFO logging so live calls are countable.
run_desk() {  # name mandate tickers
  local name=$1 mandate=$2 tickers=$3
  local out="$RECORDS/$name-$DATE.json" err="$HOME_DIR/logs/$name-$DATE.log"
  poetry run python -c "import logging, sys; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s', stream=sys.stderr); from hedge_fund.run import main; main()" \
    "$mandate" --tickers "$tickers" --effort low --out "$out" > /dev/null 2> "$err"
  local live cached
  live=$(grep -c 'hedge_fund.llm.anthropic_client' "$err" || true)
  cached=$(poetry run python -c "import json,sys; r=json.load(open('$out')); print(sum(1 for s in r['strategies'] for x in s['signals'] if x['metadata'].get('cached')))")
  echo "$name: record $out | live calls=$live cached=$cached"
}
run_desk quality-desk    "$HOME_DIR/mandates/quality-desk.yaml"     "$QUALITY"
run_desk value-desk      "$HOME_DIR/mandates/value-desk.yaml"       "$VALUE"
run_desk resilience-check "$HOME_DIR/mandates/resilience-check.yaml" "$UNION"

# 3. Ledger, candidates, scorecard.
poetry run aihf-ledger ingest "$RECORDS/quality-desk-$DATE.json" "$RECORDS/value-desk-$DATE.json" "$RECORDS/resilience-check-$DATE.json"
poetry run aihf-ledger candidates | tee "$HOME_DIR/logs/candidates-$DATE.txt"
poetry run aihf-ledger scorecard --horizon 63

# 4. Cost summary from the INFO lines (Fable 5.1 list prices), else call counts.
poetry run python - "$HOME_DIR/logs" "$DATE" <<'PY'
import re, sys, glob
logs, day = sys.argv[1], sys.argv[2]
i = o = cw = cr = n = 0
for path in glob.glob(f"{logs}/*-desk-{day}.log") + glob.glob(f"{logs}/resilience-check-{day}.log"):
    for m in re.finditer(r"input_tokens=(\d+) output_tokens=(\d+) cache_creation_input_tokens=(\d+) cache_read_input_tokens=(\d+)", open(path).read()):
        n += 1; i += int(m[1]); o += int(m[2]); cw += int(m[3]); cr += int(m[4])
cost = i * 10 / 1e6 + cw * 12.5 / 1e6 + cr * 0.25 / 1e6 + o * 50 / 1e6
print(f"cost: {n} live calls, ~${cost:.2f} (in={i} out={o} cache_write={cw} cache_read={cr})" if n else "cost: 0 live calls (all cache hits), $0.00")
PY
# 5. Names whose verdicts trail a newer filing (EDGAR companyfacts lag).
STALE=$(grep -E '^stale facts:' "$HOME_DIR/logs/candidates-$DATE.txt" | tail -1 || true)
echo "${STALE:-stale facts: none}"
echo "== done $DATE; log: $LOG =="
