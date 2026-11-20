# Architecture

```
           public peers (BTC, LTC)        local Particl node (P2P)        local BCH node (allowlisted RPC)
                    |                              |                                |
                    v                              v                                v
     +---------------------------- sources (cpp/src/source.cpp) ----------------------------+
     | header chain: Electrum files or built-in checkpoint sync; PoW, links, merkle roots     |
     +---------------------------------------------------------------------------------------+
                    |                                                       |
                    v                                                       v
     scan  (cpp/src/scan.cpp, scan_main.cpp)                   watch  (cpp/src/watch.cpp)
     parse blocks -> script spends -> templates                mempool + new blocks, early lock rules,
     -> events and tiers -> SQLite (store.cpp)                 triggers scans 45 s / 180 s after a block
                    |                                                       |
                    v                                                       v
               chainscan.sqlite                                 early_locks.csv, chainwatch_status.json
                    \                                                       /
                     \-------------------> match (match_main.cpp) <--------/
                                             ^
                       board snapshots ------+  (snapshot_main.cpp: your node's /json/offers -> snapshot files)
                                             |
                                             v
                               offer_match.csv, offer_match_early.csv
                                             |
                                             v
                         report (report_main.cpp) -> report/*.csv -> demo/make_demo.py --own -> own.html
```

## C++ runtime (`cpp/`)

One binary, `bsx-swap-tracker`, with these subcommands:

| Command | Does |
|---|---|
| `selftest` | built-in tests: hashes, varints, output types, JSON writer equal to Python, tier rules |
| `scan` | catch up every configured chain to its tip, keep the database consistent across reorgs; `--probe` and `--verify N` check connectivity and verification without writing |
| `watch` | live watcher: one connection per chain, early lock flags, scan triggers |
| `match` | offer match of completed swaps, refunds and early flags against board snapshots |
| `snapshot` | board snapshots from your own BasicSwap node (one read-only endpoint), or converted from recorded offer lists |
| `report` | aggregates: daily swaps per chain, per-chain refund share and lock-to-spend times, per-pair volume, size bands, maker shares; `--public` applies the publication rules of `datasets/` |
| `settings` | print the effective settings and where they came from |
| `dump`, `events` | parser output and recognised events for a raw block (corpus and fuzz tooling) |
| `bench` | parse speed on stored blocks |

The parts:

- **Parsers** (`tx.cpp` for blocks and transactions, `script.cpp`): Bitcoin-family transactions with segwit, BCH, Dash special transactions,
  Dogecoin AuxPoW, and Particl typed outputs and anon inputs.
- **Recognition** (`scan.cpp`): the four templates and the tiers.
- **Store** (`store.cpp`): SQLite in WAL mode. The schema is identical to the Python reference, so the two can be compared row by
  row.
- **Network** (`net.cpp`): the P2P client (version handshake that announces no services, getheaders, getdata, witness blocks) and
  the allowlisted RPC client. The RPC client refuses any method outside the allowlist before it connects.
- **Settings** (`settings.cpp`): see `configuration.md`.

## Python reference (`python/`)

This is the original proof of concept, kept as the reference implementation and the lab where new rules are tried before they are
ported. `chainscan_lib.py` is the parser and recognition, `chain_swap_scan.py` the scanner, `chain_watch.py` the watcher, and
`offer_match_live.py` the offer match with prices. `python/collectors/chain/` holds the measurement tools: recall, early-detection
study, report, corpus, shadow compare and fuzz seeds.

## Why C++ and Python both exist

C++ is the runtime: it parses the test blocks about two and a half times faster than Python, process start included (CI prints
both times), its memory is bounded, and it is fuzzed. Python is the
specification: every rule is written and measured there first, and the C++ port must reproduce it byte for byte on the corpus and
in shadow runs before it ships.
