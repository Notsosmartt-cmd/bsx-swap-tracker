#!/usr/bin/env bash
# The live part of docs/walkthrough.md as one script: probe, verify, one day of Bitcoin and Litecoin, a short watch, report,
# match and the dashboard page from your own data. Needs network access to public Bitcoin and Litecoin peers (ports 8333, 9333).
# Writes under data/ in the checkout. Takes about 6-8 minutes; WATCH_SECONDS (default 120) sets the watch step.
set -euo pipefail
cd "$(dirname "$0")/.."
B=bin/bsx-swap-tracker
D=data/analysis/chainscan
step() { printf '\n== %s\n' "$*"; }

[ -x "$B" ] || { step "build"; bash cpp/build.sh release; }
step "probe";                 $B scan --coins BTC,LTC --probe
step "verify 5 blocks";       $B scan --coins BTC,LTC --verify 5
step "scan the last day"
for try in 1 2; do                     # a scan continues where it stopped, so a second run finishes what busy peers interrupted
  out=$($B scan --coins BTC,LTC --backfill-days 1 2>&1) || true
  echo "$out" | grep -E "scanned|run finished|WARNING" | tail -20
  echo "$out" | grep -q "'BTC': 'caught_up', 'LTC': 'caught_up'" && break
  [ "$try" = 2 ] && { echo "the scan did not catch up twice; last lines:"; echo "$out" | tail -30; exit 1; }
  echo "not caught up yet: running the scan again"
done
step "watch ${WATCH_SECONDS:-120} s"; $B watch --coins BTC,LTC --seconds "${WATCH_SECONDS:-120}" --no-trigger > /dev/null 2>&1
python3 - "$D/chainwatch_status.json" <<'EOF'
import json, sys
st = json.load(open(sys.argv[1]))
for coin in ("BTC", "LTC"):
    c = st["coins"][coin]
    print(f"{coin}: peer {'yes' if c['peer'] else 'none'}, {c['txs']} mempool txs, {c['blocks']} blocks, {c['tx_parse_errors']} parse errors, {c['flags']} flags")
    assert c["tx_parse_errors"] == 0, coin + ": parse errors"
assert any(st["coins"][c]["txs"] or st["coins"][c]["blocks"] for c in ("BTC", "LTC")), "the watcher saw no transactions at all"
EOF
step "report";                $B report
python3 - "$D/report/chains.csv" <<'EOF'
import csv, sys
rows = {r["chain"]: r for r in csv.DictReader(open(sys.argv[1]))}
for chain in ("BTC", "LTC"):
    r = rows.get(chain)
    print(f"{chain}: {r['completed'] if r else 0} completed, {r['refunded'] if r else 0} refunded, lock to spend p50 {r['lock_to_spend_min_p50'] if r else '-'} min")
assert "BTC" in rows and int(rows["BTC"]["completed"]) > 0, "no completed Bitcoin swaps in a day: unusual, check the scan log"
EOF
step "match (no order-book snapshots: nothing to match is expected)"; $B match
step "dashboard page";        python3 demo/make_demo.py --own "$D/report"
step "walkthrough passed"
