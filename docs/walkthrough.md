# Walkthrough

This takes you from a fresh clone to your own trade data in about 15 minutes. Every command here was run on a clean checkout
with an empty home directory and only the system Python; the timings are from that run (a 16-core Linux box on a home
connection). `ci/walkthrough.sh` runs the live steps in one go, and GitHub Actions runs it on a clean Ubuntu 24.04 machine.

If you only want to look: `samples/2026-11-20/` is the full output of one such run (every trade with its transaction ids) and
`reports/index.html` charts it next to the six-month datasets.

## 1. Install and build

Linux (tested on Ubuntu 24.04). On Debian or Ubuntu:

```
sudo apt-get install -y git cmake g++ libsqlite3-dev zlib1g-dev libssl-dev python3
git clone https://github.com/Notsosmartt-cmd/bsx-swap-tracker.git
cd bsx-swap-tracker
bash cpp/build.sh release
```

On Fedora the packages should be `git cmake gcc-c++ sqlite-devel zlib-devel openssl-devel python3` (not tested). Ninja is used when it's installed
and make otherwise. The build takes about a minute and ends with `installed bin/bsx-swap-tracker (<commit>)`. Nothing else needs
installing: the Python parts use only the standard library.

## 2. Offline checks

```
bash ci/run_checks.sh
```

This needs no network. It runs the binary's self-test, checks the 19 real mainnet blocks in `fixtures/` against their
checksums, parses them with both the C++ binary and the Python reference (the outputs must be identical), runs the Python
tests and the watcher's self-test, rebuilds the demo, and checks for leaked paths and non-ASCII text. It should end with
`== all checks passed` after a few seconds.

Then open `demo/index.html` in a browser. The top half is real (the aggregate datasets in `datasets/`); the bottom half is a
synthetic market that shows the maker views without publishing anyone's trades.

## 3. Reach the networks

```
bin/bsx-swap-tracker scan --coins BTC,LTC --probe
```

The first run downloads block headers from a checkpoint built into the binary (May 2026) and checks each one's proof of work:
about 26,000 for Bitcoin and 102,000 for Litecoin. That took 102 seconds. You should see

```
BTC  tip 976761, tip block 2026-11-20T17:29:14Z (0.2 h old)
LTC  tip 3216416, tip block 2026-11-20T17:38:14Z (0.1 h old)
```

Lines like `LTC: header sync peer lost at height 3204000 (peer closed the connection), trying another` are normal. Public nodes
drop new connections when they are full; the sync keeps what it has and carries on with another peer. Later runs only fetch new
headers and take a few seconds.

```
bin/bsx-swap-tracker scan --coins BTC,LTC --verify 5
```

Fetches the last 5 blocks of each chain and checks every transaction hash, the proof of work and the merkle root. Writes nothing.
About a minute.

## 4. Scan a day

```
bin/bsx-swap-tracker scan --coins BTC,LTC --backfill-days 1
```

About 145 Bitcoin and 577 Litecoin blocks, 2.5 minutes, 180 MB of memory, 121 MB on disk. A later `scan` without `--backfill-days` continues from
where the database ends. A new database without the flag starts 7 days back; `--backfill-days max` goes as far back as the peers
serve, about 180 days of Bitcoin and several GB. The log prints a line for every block
with a recognised spend. Most of them say `candidate`: those are ordinary 2-of-2 multisig spends (Lightning channels, exchange
wallets) that share the script shape. The ones that matter are `adaptor_lock_spend/strong` (a completed trade) and
`adaptor_refund_tx/certain` (a failed one). The run ends with `run finished: {'BTC': 'caught_up', 'LTC': 'caught_up'}`.

To see what was recorded:

```
python3 - <<'EOF'
import sqlite3
db = sqlite3.connect("data/analysis/chainscan/chainscan.sqlite")
for row in db.execute("select coin, kind, confidence, count(*) from swap_events group by 1, 2, 3 order by 1, 4 desc"):
    print(*row)
EOF
```

On 20 November that gave 33 completed trades and 1 refund on Bitcoin, 9 and 2 on Litecoin. The rows:

- `adaptor_lock_spend strong`: a completed BasicSwap trade. This is what `report` counts.
- `adaptor_refund_tx certain`: a failed trade, the refund path being opened. `adaptor_refund_spend certain` is that refund output
  being spent later (the refund itself, or rarely the penalty branch), so it isn't counted again.
- `adaptor_lock_spend likely` and `candidate`: the same 2-of-2 script without all the fields BasicSwap sets. Mostly Lightning and
  multisig wallets; not counted.
- `htlc_redeem certain`: hash-locked swaps of any protocol. Context, not BasicSwap volume.

## 5. Watch live

```
bin/bsx-swap-tracker watch --coins BTC,LTC --seconds 300 --no-trigger
```

One connection per chain; it reads every new mempool transaction and block (about 10 MB of memory). At the end it prints its
status: peers, transactions seen, blocks, parse errors (should be 0) and flags. While it runs, `data/analysis/chainscan/chainwatch_status.json`
holds the same.

Flags will be 0, and that's expected. Before a trade settles, its lock is a plain script-hash output that says nothing about
what's inside. The watcher can only tell it's a trade because its amount equals a quote on the order book to the satoshi, so
early detection needs order-book snapshots from a BasicSwap node (step 7). `--no-trigger` stops the watcher from trying to start
the scan units that `deploy/systemd/` installs; leave it out once those are installed.

## 6. Aggregates and your own dashboard

```
bin/bsx-swap-tracker report
python3 demo/make_demo.py --own data/analysis/chainscan/report
```

`report` writes `daily_swaps.csv`, `chains.csv`, `pairs.csv`, `sizes.csv` and `makers.csv` to `data/analysis/chainscan/report/`.
The second command turns them into `demo/own.html`, the same page as the demo with your data in it.

## 7. With a BasicSwap node

If you run a BasicSwap node, record its order book and match trades to quotes:

```
bin/bsx-swap-tracker snapshot --url http://127.0.0.1:12700 --interval 60 --count 0
bin/bsx-swap-tracker match
```

Leave `snapshot` running (one read-only request a minute to your own node; it refuses anything but a loopback address). `match`
then ties each completed trade, refund and early flag to the quote it filled. That adds the pair, the direction, the price, the
maker and the quote's age, and fills in `pairs.csv` and `makers.csv`. Without snapshots `match` reports `0 offers from snapshots`
and writes empty files.

## What to look for in the data

- Completed trades per day. Bitcoin had a median of 4 a day in June to August, 8 in September and October, and 38 in the ten
  days to 19 November, peaking at 80 (`datasets/daily_swaps.csv`). Litecoin runs at about 3 to 5 a day, with one outlier: 94 on
  24 August. A full day of your own scan should look like the most recent days there.
- Refund share. Since May it's 6.0% on Bitcoin and 7.2% on Litecoin (`refund_pct` in `chains.csv`). On Bitcoin it fell from
  13% in May to August to 3% since September and 1% in November (`docs/validation.md`). On a single day it's noise: 2 refunds out of 11 trades is 18%.
- Lock to spend. The median is 15 to 20 minutes on Bitcoin and Litecoin, with a long tail on Bitcoin (p90 about 56 minutes).
  That's how long the two parties have funds committed; hours would mean something is off.
- Sizes (`sizes.csv`). Most Bitcoin locks are 0.001 to 0.01 BTC, a few hundred dollars. About 6% (97 trades) are 1 to 10 BTC:
  repeated fills of large Monero quotes. The two groups are different businesses. Trades whose lock amount is unknown (the lock
  came before the scan started) are left out, so the bands add up to slightly less than `chains.csv`.
- `secret_hash_redeems_any_protocol` in `daily_swaps.csv` counts hash-locked swaps of every protocol, not only BasicSwap. It's
  context, not volume.
- The start of a fresh scan. A trade whose lock transaction is older than your scan window gets the benefit of the doubt, so the
  first hours can hold one or two trades rated higher than a longer scan would rate them. Compared with a six-month scan of the
  same blocks, one fresh day agreed on 98 of 99 events, and the 99th was this case.

## When something goes wrong

- `report: no swap database at ... yet`: run `scan` first.
- `LTC not reachable: LTC header sync: ...` after six peers: the public peers were busy. Run it again; it continues where it stopped.
- `error: unknown flag`: every command takes `--help`.
- A crash or a parse error on real data: please open an issue with the block hash. `bin/bsx-swap-tracker dump btc <raw block file>`
  shows what the parser makes of a block.
