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
# Ledger cutoff, baked in rather than inherited.
#
# 2026-09-20 is the date the universe widened from --limit 10 to the whole
# screen. Verdicts before it grade schools on ten alphabetically-first
# names, against a universe bar built from a screen that no longer exists,
# so they are not comparable with anything after it. The 100 rows from the
# 2026-09-15 pilot stay in the ledger as history and simply fall outside
# this window.
#
# It is a literal here on purpose. This script runs from a LaunchAgent with
# its own environment: it never sees a shell profile, so a cutoff exported
# in a terminal would be silently absent on Monday and September would
# quietly rejoin the scorecard and the playbook. AIHF_LEDGER_SINCE still
# overrides for a one-off.
SINCE=${AIHF_LEDGER_SINCE:-2026-09-20}
SINCE_ARG=()
[ -n "$SINCE" ] && SINCE_ARG=(--since "$SINCE")

# A spend ceiling for THIS script only — deliberately exported here rather
# than set in ~/.hedge-fund/.env, which apply_credentials() loads into every
# invocation. A manual seeding run must not inherit the weekly ceiling and
# discover it as a refusal.
#
# $10, and here is what the number is anchored to.
#
# Earnings cluster hard. Across four years of filing history for the 78
# names in today's two screens, the busiest single week re-prices 34 of
# them — 174 calls, about $4.67. The previous $3 default would have refused
# that week, and under `set -e` aborted before the ingest: the gate
# becoming the outage. $10 clears the observed peak roughly twice over
# while still refusing a full invalidation of all three desks (~$10.50),
# which is the runaway actually worth catching.
#
# It is conditioned on a universe of roughly this size (quality 55, value
# 24, union 78). Screen membership churns as fundamentals move names in and
# out, so re-derive it if the screens grow materially; the method is in
# ARCHITECTURE.md §12.
export HEDGE_FUND_MAX_COST=${AIHF_WEEKLY_MAX_COST:-10.00}

# Failure has to reach a human. This script runs unattended from a
# LaunchAgent, and `set -e` means a refused desk aborts everything after it
# — including the ingest and the scorecard. Silently, into a log nobody
# opens. So: leave a marker on any non-zero exit, and report any marker
# left by an earlier run at the top of the next one.
FAILED_MARKER="$HOME_DIR/logs/LAST-RUN-FAILED"
on_exit() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    { echo "weekly run FAILED on $DATE (exit $code)"
      echo "log: $LOG"
      # The failure worth naming, because the pre-flight cannot see it.
      # --max-cost measures what a run WILL cost; it knows nothing about
      # what the account has left, so a run can estimate $1.87, pass the
      # gate, and die on the first call against an exhausted Console
      # usage limit. That happened on 2026-09-20. The SDK returns the
      # reset date in the 400, so quote it rather than making the next
      # reader find it in a traceback.
      local limit
      limit=$(grep -ho 'You have reached your specified API usage limits[^"}]*' \
                "$HOME_DIR/logs/"*-"$DATE".log 2>/dev/null | head -1 | tr -d "'\"" || true)
      if [ -n "$limit" ]; then
        echo "CAUSE: Anthropic account usage limit reached — $limit"
        echo "  (this is the Console cap on the account, NOT --max-cost;"
        echo "   raise it in the Anthropic Console, or wait for the reset above)"
      elif [ "$code" -eq 2 ]; then
        echo "CAUSE: a desk exceeded HEDGE_FUND_MAX_COST=$HEDGE_FUND_MAX_COST"
        echo "  (local pre-flight gate; raise AIHF_WEEKLY_MAX_COST if the"
        echo "   universe grew, and re-derive per ARCHITECTURE.md §12)"
      fi
    } > "$FAILED_MARKER"
    echo "== WEEKLY-RUN-FAILED $DATE exit=$code — see $LOG =="
  else
    rm -f "$FAILED_MARKER"
  fi
}
trap on_exit EXIT
LOG="$HOME_DIR/logs/weekly-$DATE.log"
RECORDS="$HOME_DIR/records"
mkdir -p "$HOME_DIR/logs" "$RECORDS"
exec > >(tee -a "$LOG") 2>&1

cd "$REPO"
echo "== weekly $DATE =="

# Anything left behind by a previous run that died.
if [ -f "$FAILED_MARKER" ]; then
  echo "== WEEKLY-RUN-FAILED (earlier run) =="
  sed 's/^/   /' "$FAILED_MARKER"
  echo "== end of earlier failure =="
fi

# 1. Universes — the WHOLE screen, no --limit.
#
# --limit 10 truncated an already-downloaded list in export order, which
# Finviz returns alphabetically. The desk was evaluating the ten
# alphabetically-first names that passed each screen and never seeing the
# other 46 (quality) or 14 (value) — a permanent slice of A-F, not the ten
# best by any criterion. Taking everything removes the need for a sort key
# at all; HEDGE_FUND_MAX_COST above is the backstop if a preset ever starts
# matching hundreds.
#
# A failed, empty, or throttled screen must never reach aihf.
QUALITY=$(poetry run aihf-universe quality)
VALUE=$(poetry run aihf-universe value)
[ -n "$QUALITY" ] || { echo "quality universe is empty; stopping" >&2; exit 1; }
[ -n "$VALUE" ] || { echo "value universe is empty; stopping" >&2; exit 1; }
UNION=$( (tr ',' '\n' <<<"$QUALITY"; tr ',' '\n' <<<"$VALUE") | awk 'NF && !seen[$0]++' | paste -sd, -)
echo "quality: $QUALITY"
echo "value:   $VALUE"
echo "union:   $UNION"

# Preset turnover, measured rather than estimated.
#
# The whole accrual timeline rests on how much the screens churn, and the
# only estimate so far came from reasoning about the filters: fundamental
# thresholds move on quarterly filings, so turnover "should" be near zero.
# Then the quality screen went 55 -> 56 in five days. One name is not much,
# but it is not zero, and the structural argument cannot tell the
# difference. This accumulates the real series instead.
UNIVERSE_STATE="$HOME_DIR/logs/universe"
mkdir -p "$UNIVERSE_STATE"
diff_universe() {  # preset current_csv
  local preset=$1 current=$2 prev="$UNIVERSE_STATE/$1.txt"
  printf '%s\n' "$current" | tr ',' '\n' | sort > "$UNIVERSE_STATE/.$preset.new"
  if [ -f "$prev" ]; then
    local added dropped
    added=$(comm -13 "$prev" "$UNIVERSE_STATE/.$preset.new" | paste -sd, -)
    dropped=$(comm -23 "$prev" "$UNIVERSE_STATE/.$preset.new" | paste -sd, -)
    echo "turnover $preset: $(wc -l < "$prev" | tr -d ' ') -> $(wc -l < "$UNIVERSE_STATE/.$preset.new" | tr -d ' ')" \
         "| added: ${added:-none} | dropped: ${dropped:-none}"
    # One line per run, appended, so the series is greppable later.
    echo "$DATE,$preset,$(wc -l < "$UNIVERSE_STATE/.$preset.new" | tr -d ' '),${added:-},${dropped:-}" \
      >> "$UNIVERSE_STATE/turnover.csv"
  else
    echo "turnover $preset: first run, $(wc -l < "$UNIVERSE_STATE/.$preset.new" | tr -d ' ') names, no baseline yet"
    echo "$DATE,$preset,$(wc -l < "$UNIVERSE_STATE/.$preset.new" | tr -d ' '),," >> "$UNIVERSE_STATE/turnover.csv"
  fi
  mv "$UNIVERSE_STATE/.$preset.new" "$prev"
}
[ -f "$UNIVERSE_STATE/turnover.csv" ] || echo "date,preset,size,added,dropped" > "$UNIVERSE_STATE/turnover.csv"
diff_universe quality "$QUALITY"
diff_universe value "$VALUE"

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
# Each run above already logged its own verdicts inline. This ingest is
# deliberate belt-and-braces for an unattended job: it is idempotent
# (keyed on school/ticker/snapshot_hash) so it normally reports
# added=0, and it catches up if inline logging ever fails.
poetry run aihf-ledger ingest "$RECORDS/quality-desk-$DATE.json" "$RECORDS/value-desk-$DATE.json" "$RECORDS/resilience-check-$DATE.json"
poetry run aihf-ledger candidates "${SINCE_ARG[@]}" | tee "$HOME_DIR/logs/candidates-$DATE.txt"
# Scorecard. The three desks above are passed explicitly so coverage
# reports "is this school accumulating calls" rather than "could some
# mandate on disk run it" — without them every unrun mandate counts as
# staffing and the ad-hoc schools are hidden among the provisional ones.
poetry run aihf-ledger scorecard --horizon 63 "${SINCE_ARG[@]}" \
  --mandate "$HOME_DIR/mandates/quality-desk.yaml" \
  --mandate "$HOME_DIR/mandates/value-desk.yaml" \
  --mandate "$HOME_DIR/mandates/resilience-check.yaml"

# 4. Cost summary from the INFO lines. Rates come from hedge_fund.llm.pricing,
# the same table the pre-flight estimate uses — not a second copy here.
poetry run python - "$HOME_DIR/logs" "$DATE" <<'PY'
import re, sys, glob
logs, day = sys.argv[1], sys.argv[2]
i = o = cw = cr = n = 0
for path in glob.glob(f"{logs}/*-desk-{day}.log") + glob.glob(f"{logs}/resilience-check-{day}.log"):
    for m in re.finditer(r"input_tokens=(\d+) output_tokens=(\d+) cache_creation_input_tokens=(\d+) cache_read_input_tokens=(\d+)", open(path).read()):
        n += 1; i += int(m[1]); o += int(m[2]); cw += int(m[3]); cr += int(m[4])
from hedge_fund.llm.pricing import price_for   # one copy of the rates
p = price_for("claude-fable-5-1")
cost = (i * p.input + cw * p.cache_write + cr * p.cache_read + o * p.output) / 1e6
print(f"cost: {n} live calls, ~${cost:.2f} (in={i} out={o} cache_write={cw} cache_read={cr})" if n else "cost: 0 live calls (all cache hits), $0.00")
PY
# 5. Names whose verdicts trail a newer filing (EDGAR companyfacts lag).
STALE=$(grep -E '^stale facts:' "$HOME_DIR/logs/candidates-$DATE.txt" | tail -1 || true)
echo "${STALE:-stale facts: none}"
echo "== done $DATE; log: $LOG =="
