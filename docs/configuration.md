# Configuration

C++ and Python read the same TOML file with the same keys and defaults. `python/collectors/tests/test_collector_settings.py`
checks that both resolve identical settings.

## Where the file is looked for

The first match wins:

1. `--config FILE`, a global flag placed before the subcommand: `bsx-swap-tracker --config my.toml scan ...`
2. the environment variable `BSX_SWAP_TRACKER_CONFIG`
3. `<root>/deploy/bsx-swap-tracker.toml`
4. `<root>/config/bsx-swap-tracker.toml`
5. built-in defaults: everything under `<root>/data/`, and BasicSwap's files under `<root>/coindata/`

`<root>` is the repository root, found by walking up from the binary. A file named explicitly (with `--config` or the
environment variable) that cannot be read is an error. Unknown keys and invalid ports are errors too, so a typo cannot silently
disable a safety setting. Check the result with `bsx-swap-tracker settings` or `python3 python/collectors/collector_settings.py`.

## Keys

See `config/example.toml`.

| Key | Default | Used for |
|---|---|---|
| `paths.root` | repository root | base for relative paths |
| `paths.data_dir` | `data/analysis/chainscan` | database, status files, early locks, header stores |
| `paths.snapshots_dir` | `data/basicswap/site_snapshots` | board snapshots (`<UTC>Z.json.gz`) for matching and early detection |
| `paths.logs_dir` | `data/logs` | rotating logs |
| `paths.locks_dir` | `data/locks` | single-instance heartbeat locks, shared by C++ and Python |
| `paths.basicswap_json` | `coindata/basicswap.json` | BasicSwap's config, read only for the BCH node's RPC credentials (never logged) |
| `paths.electrum_headers_dir` | `coindata/electrum_headers` | BasicSwap's Electrum header files; when absent, headers are synced from built-in checkpoints |
| `safety.protected_ports` | `[]` | ports the tracker must never contact, added to BasicSwap's defaults |

BasicSwap's default ports are always protected: UI 12700, WebSocket 11700, Particl RPC 19792 and ZMQ 20792, Monero wallet 29998,
Wownero wallet 34798, Decred wallet 9209. List every other wallet RPC or UI on your machine under `safety.protected_ports`.

## Command-line overrides

- `scan` and `watch`: `--data-dir`, `--snapshots-dir`, `--coins`.
- `match`: `--days`, `--context-days`, `--out-dir`, `--db`.
- `report`: `--db`, `--match FILE` (default `<data_dir>/offer_match.csv`), `--out-dir` (default `<data_dir>/report`), `--public`.
- `snapshot`: `--url http://127.0.0.1:PORT` (your own node; there is no default), `--interval S` (15 s minimum), `--count N`,
  `--from-jsonl FILE`.

## Board snapshots

The matcher needs offers. There are two sources:

- **Your own BasicSwap node:** `bsx-swap-tracker snapshot --url http://127.0.0.1:12700 --interval 60 --count 0` writes one snapshot
  a minute. This is the only place the program talks to a BasicSwap UI port. It sends one request shape only: `POST /json/offers`
  with a fixed read-only filter, to an explicitly named loopback address. Your node sees offers from the networks it is connected
  to, so its view can differ slightly from other boards'.
- **Recorded offer lists** (one `{"ts": ..., "offers": [...]}` per line, as returned by `/json/offers`):
  `bsx-swap-tracker snapshot --from-jsonl offers.jsonl.gz`.

Fields the node does not expose (lock type and value, protocol version) are left empty in converted snapshots. The match columns
that depend on them stay empty too.
