#!/usr/bin/env bash
# Run every libFuzzer target for a fixed time, one after another (CPU heavy: on a shared machine run it at low priority):
#   bash cpp/fuzz/run.sh 300
# Corpus (grows between runs), seeds and crash artefacts live in <data_dir>/fuzz/ (git-ignored); BSX_FUZZ_DIR overrides it.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$here/../../python/collectors" ]; then root="$(cd "$here/../.." && pwd)"; else root="$(git -C "$here" rev-parse --show-toplevel 2>/dev/null || (cd "$here/../../.." && pwd))"; fi
secs="${1:-300}"
# data_dir from the settings module (same lookup as the binary and the Python tools), in either repository layout
cs="$root/python/collectors/collector_settings.py"; [ -f "$cs" ] || cs="$root/collectors/collector_settings.py"
F="${BSX_FUZZ_DIR:-$(cd "$root" && python3 "$cs" | awk '$1 == "data_dir" {print $2}')/fuzz}"
mkdir -p "$F/artifacts"
for t in fuzz_block fuzz_tx fuzz_script fuzz_json fuzz_frame; do
  mkdir -p "$F/corpus/$t" "$F/seeds/${t#fuzz_}"
  echo "== $t ($secs s)"
  "$here/../build-fuzz/$t" -max_total_time="$secs" -rss_limit_mb=2048 -max_len=700000 -timeout=20 -print_final_stats=1 \
      -artifact_prefix="$F/artifacts/$t-" "$F/corpus/$t" "$F/seeds/${t#fuzz_}" 2>&1 \
    | grep -E "^#[0-9]+.*(DONE|INITED)|stat::number_of_executed_units|stat::peak_rss_mb|ERROR|SUMMARY|crash-|leak-|timeout-|oom-" | tail -8
done
echo "artifacts (crashes, leaks, timeouts): $(ls "$F/artifacts" | wc -l)"
