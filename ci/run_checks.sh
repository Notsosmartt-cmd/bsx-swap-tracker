#!/usr/bin/env bash
# All checks of the repository, as CI runs them (and as you can run them locally from the repository root):
#   C++ release build + self-test, settings resolution, fixture integrity, corpus parity (parser dump and events, C++ = Python),
#   Python unit tests, chain-watch self-test, demo build, ASCII check of the docs and code.
# Optional: ASAN=1 also builds with AddressSanitizer/UBSan and replays the corpus; FUZZ_SECONDS=N runs each fuzz target N s (clang).
set -euo pipefail
cd "$(dirname "$0")/.."
step() { printf '\n== %s\n' "$*"; }
step "build (release)"
bash cpp/build.sh release
BIN=bin/bsx-swap-tracker
step "self-test";            "$BIN" selftest
step "settings";             "$BIN" settings
step "fixture integrity";    (cd fixtures && sha256sum -c --quiet SHA256SUMS && echo "fixtures ok")
step "corpus parity";        python3 python/collectors/chain/cpp_corpus.py --corpus fixtures/corpus --compare "$BIN" --events | tail -1
python3 python/collectors/chain/cpp_corpus.py --corpus fixtures/corpus --compare "$BIN" --events | grep -q " 0 different"
step "python tests"
for t in python/collectors/tests/test_*.py; do python3 "$t"; done
step "chain-watch self-test"; python3 python/collectors/chain_watch.py --selftest
step "demo";                  python3 demo/make_demo.py && git diff --quiet -- demo/index.html datasets/synthetic 2>/dev/null || echo "note: demo output differs from the committed copy (or not a git checkout)"
step "reports";               python3 reports/make_reports.py && git diff --quiet -- reports/index.html 2>/dev/null || echo "note: reports/index.html differs from the committed copy (or not a git checkout)"
python3 reports/verify_reports.py | tail -1
step "privacy (no home paths, e-mail addresses or non-local IPs in the repository; chain fixtures and your own data/ excluded)"
leaks=$(grep -rIlE '/home/[a-z]|/Users/[A-Za-z]|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.](com|net|org|io|me|dev)\b' --exclude-dir=.git --exclude-dir=corpus --exclude-dir=bin --exclude-dir='build-*' --exclude-dir=data --exclude-dir=coindata --exclude=own.html . || true)
ips=$(grep -rInE '\b([0-9]{1,3}[.]){3}[0-9]{1,3}\b' --exclude-dir=.git --exclude-dir=corpus --exclude-dir=synthetic --exclude-dir=bin --exclude-dir='build-*' --exclude-dir=data --exclude-dir=coindata --exclude=own.html . | grep -v 'export-ok' \
      | while IFS= read -r ln; do echo "$ln" | grep -oE '\b([0-9]{1,3}[.]){3}[0-9]{1,3}\b' | grep -qvE '^(127[.]|0[.]0[.]0[.]0$)' && echo "$ln"; done || true)
if [ -n "$leaks$ips" ]; then echo "privacy check failed:"; echo "$leaks"; echo "$ips"; exit 1; fi; echo "privacy ok"
step "ASCII";                 ! grep -rIlP '[^\x00-\x7F]' --include='*.md' --include='*.py' --include='*.cpp' --include='*.hpp' --include='*.sh' --include='*.toml' . || { echo "non-ASCII found"; exit 1; }
if [ "${ASAN:-0}" = 1 ]; then
  step "sanitizer build + corpus"; bash cpp/build.sh asan
  python3 python/collectors/chain/cpp_corpus.py --corpus fixtures/corpus --compare cpp/build-asan/bsx-swap-tracker --events | tail -1
fi
if [ "${FUZZ_SECONDS:-0}" -gt 0 ]; then
  step "fuzz smoke (${FUZZ_SECONDS} s per target)"; bash cpp/build.sh fuzz
  BSX_CORPUS_DIR=fixtures/corpus python3 python/collectors/chain/cpp_fuzz_seeds.py && bash cpp/fuzz/run.sh "$FUZZ_SECONDS"
fi
step "all checks passed"
