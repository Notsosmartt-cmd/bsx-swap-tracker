#!/usr/bin/env bash
# Build the C++ swap tracker (about a minute on one core, under 1 GB of memory; uses Ninja when installed, else make):
#   bash cpp/build.sh release
# Variants: release -> bin/bsx-swap-tracker (+ bin/bsx-swap-tracker.version), asan -> cpp/build-asan/bsx-swap-tracker (tests only),
#           fuzz -> cpp/build-fuzz/fuzz_* (libFuzzer targets; run with cpp/fuzz/run.sh).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# repository root in either layout: <root>/cpp (public bsx-swap-tracker) or <root>/cpp/chainscan (a larger checkout that embeds it)
if [ -d "$here/../python/collectors" ]; then root="$(cd "$here/.." && pwd)"; else root="$(git -C "$here" rev-parse --show-toplevel 2>/dev/null || (cd "$here/../.." && pwd))"; fi
kind="${1:-release}"
case "$kind" in
  release) bdir="$here/build-release"; extra=(-DCMAKE_BUILD_TYPE=Release) ;;
  asan)    bdir="$here/build-asan";    extra=(-DCMAKE_BUILD_TYPE=Debug -DBSX_ASAN=ON) ;;
  fuzz)    bdir="$here/build-fuzz";    extra=(-DCMAKE_BUILD_TYPE=Debug -DBSX_FUZZ=ON -DCMAKE_CXX_COMPILER=clang++) ;;
  *) echo "usage: build.sh [release|asan|fuzz]"; exit 2 ;;
esac
if [ -f "$bdir/CMakeCache.txt" ]; then gen=(); elif command -v ninja >/dev/null 2>&1; then gen=(-G Ninja); else gen=(-G "Unix Makefiles"); fi
cmake -S "$here" -B "$bdir" "${gen[@]}" "${extra[@]}" >/dev/null
cmake --build "$bdir" -j 2
if [ "$kind" = release ]; then
  mkdir -p "$root/bin"
  install -m 0755 "$bdir/bsx-swap-tracker" "$root/bin/bsx-swap-tracker"
  { git -C "$root" log -1 --format=%H -- cpp/ 2>/dev/null || echo unknown; } > "$root/bin/bsx-swap-tracker.version"
  [ -z "$(git -C "$root" status --porcelain -- cpp/ 2>/dev/null)" ] || echo "dirty" >> "$root/bin/bsx-swap-tracker.version"
  echo "installed bin/bsx-swap-tracker ($(cat "$root/bin/bsx-swap-tracker.version" | tr '\n' ' '))"
fi
