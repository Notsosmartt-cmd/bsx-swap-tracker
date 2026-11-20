# Operations

## Build

```
bash cpp/build.sh release        # or: cmake -S cpp -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build
bash cpp/build.sh asan           # sanitizer build (tests and corpus)
bash cpp/build.sh fuzz           # libFuzzer targets (clang)
```

You need a C++20 compiler, CMake, Ninja (optional), and the SQLite, zlib and OpenSSL (libcrypto) development packages. A release
build takes about a minute on one core and under 1 GB of memory.

## First run

```
bin/bsx-swap-tracker selftest
bin/bsx-swap-tracker settings
bin/bsx-swap-tracker scan --coins BTC,LTC --probe       # can the sources be reached? (no writes)
bin/bsx-swap-tracker scan --coins BTC,LTC --verify 5    # fetch and fully verify 5 blocks per chain (no writes)
bin/bsx-swap-tracker scan --coins BTC --backfill-days 1 # first real scan
```

With no Electrum header files, the first BTC/LTC run syncs headers from the built-in checkpoint (2026-05). That takes about 40 s for Bitcoin and 1 to 2 minutes for
Litecoin, and 30 MB of memory, then is incremental.

## Running continuously

Each command does one job and exits, except `watch`. A live picture (new trades stored within minutes of a block, early flags while
a trade settles, quotes recorded every minute) needs them run on a schedule and the watcher kept up. The systemd user units below
are one way to do that; cron or any process supervisor works as well. Nothing needs them for a one-off scan.

`deploy/systemd/` has user-unit templates:

| Unit | Runs |
|---|---|
| `bsx-swap-scan@<COIN>.timer` / `.service` | `scan` of one chain every 10 minutes (a backstop; the watcher triggers scans after blocks) |
| `bsx-swap-watch.service` | `watch` (restarts on failure) |
| `bsx-swap-snapshot.service` | `snapshot --url ... --interval 60 --count 0` from your own node (optional) |
| `bsx-swap-match.timer` / `.service` | `match` every 10 minutes, then `report` |

Install with `deploy/systemd/install.sh`. It replaces `@ROOT@` with the checkout path, copies the units to
`~/.config/systemd/user/`, and enables nothing by itself. The units run at idle CPU and IO priority, with a memory cap.

## Resource use

Measured on the author's host:

- **Scan:** memory is bounded by the block size plus the index. A caught-up run takes seconds of CPU per chain.
- **Watch:** one TCP connection per chain, 10 to 25 MB of memory, under 1% of a core.
- **Backfill:** without `--backfill-days`, a new database starts 7 days back (about 1,000 Bitcoin blocks) and an existing one only
  moves forward. A large backfill (`--backfill-days max`, about 180 days on BTC) reads about 25,000 blocks from peers and writes a
  database of several GB. Run it at idle priority, with a memory cap, on a machine with headroom.

## Health

- `<data_dir>/status.json` holds per-chain state (`caught_up`, `unreachable`, gaps).
- `<data_dir>/chainwatch_status.json` holds the watcher's connections, last block and flags.
- Each component writes a heartbeat file. A supervisor (systemd `Restart=`, or your own watchdog) can restart a component whose
  heartbeat is stale.
- Logs go to `<logs_dir>/` (rotating, 5 x 5 MB).

## Upgrading

The database schema is shared with the Python reference and changes only with a migration. A change of the tier rules can be
applied to stored events by rescanning a height range. A one-off reclassification can be done in SQL, provided you keep a log of
the changed rows.
