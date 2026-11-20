#!/usr/bin/env bash
# Render the unit templates with this checkout's path and copy them to ~/.config/systemd/user/. Enables and starts nothing.
# Then, for example:  systemctl --user enable --now bsx-swap-watch.service bsx-swap-scan@BTC.timer bsx-swap-scan@LTC.timer bsx-swap-match.timer
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
dst="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; mkdir -p "$dst"
for f in "$here"/*.service "$here"/*.timer; do sed "s|@ROOT@|$root|g" "$f" > "$dst/$(basename "$f")"; echo "installed $(basename "$f")"; done
systemctl --user daemon-reload
