# bsx-swap-tracker

I built this for market making on BasicSwap, a peer-to-peer exchange. Listed liquidity is reported and visible but not volume or
open interest. I needed to know more about size, pricing, and other makers on the books for accurate modeling.

This tool rebuilds the missing trade record from public settlement data, ties trades back to the public quotes they filled, and
live flags trades while they are still settling, a median 19 minutes before they complete.

Over six months it has reconstructed 2,533 completed trades and 174 failed settlements on three chains. On Bitcoin it finds 537 of the 539 trades that can be checked against a public quote (99.6%).

## Problem

There's no central exchange, so nobody publishes trades. Each trade settles directly between the two parties as an atomic swap
across two blockchains. Most trades are against Monero, whose side is private; the other side looks like an ordinary 2-of-2
multisig payment on Bitcoin, Litecoin or Particl, with nothing linking it to BasicSwap or to a quote. The tracker sees only that
scripted side, so a "trade" here is one swap on that chain, whatever the other coin was (see Limitations).

## Use cases

Every completed trade comes with its chain, size and time. Since 10 October, 63% of completed Bitcoin trades (510 of 807) match exactly one public
quote, which adds the pair, the quote's rate, its age and the maker. From that you can measure:

* fill rates by quote size and premium to Kraken, the input a quoting model needs and the venue doesn't publish
* realized pricing (since 10 October, takers buying XMR paid a median 3.1% over Kraken; takers selling it got 1.1% more)
* adverse selection, from each quote's premium when posted versus at fill (the median moves 0.02 points; the tails are the pick-offs)
* each maker's share of volume and how often they repost (one address carries about a third of matched volume)
* which quotes end in failed settlements (6% of Bitcoin trades since May, down from 13% in the summer to 3% since September; 7% on Litecoin)
* time from commitment to settlement (median 19 minutes on Bitcoin, 15 on Litecoin), which sets your hedge horizon
* when your own quote is being filled, so you can hedge at the counterparty's lock instead of waiting for settlement
* a fill history to backtest quoting strategies against, after hedge costs and failed settlements

More detail, with the numbers behind these, is in `docs/market-making.md`.

## What it does

It's one binary with five commands (`bin/bsx-swap-tracker <command> --help` lists the flags). Each can run on its own, or all of
them under systemd.

* `scan` reads every block on Bitcoin, Litecoin, Particl and Bitcoin Cash, checks it, and records each completed swap and refund
in SQLite.
* `watch` is the live watcher. It holds one connection per chain, reads the mempool and new blocks, and flags a swap lock the  
moment it appears if its amount equals a live quote (early detection: on Bitcoin over six days in November, 72% of trades were flagged before they settled, a median 19 minutes ahead). That needs order-book snapshots. With the systemd units installed it also starts a scan after each block.
* `snapshot` records the order book from your own BasicSwap node with one read-only API call, once or on a timer.
* `match` ties completed trades, refunds and early flags to the quotes they filled, which adds the pair, the quote's rate and the maker.
A lock that fits more than one quote, or whose amount is round (a multiple of 1,000 base units, which matches quotes by chance), is
kept in the match file but left out of the pair, maker and USD totals.
* `report` builds the aggregates: daily trades per chain, refund share and settlement times per chain, volume per pair, size
bands and maker shares. `--public` adds the rounding and minimum-count rules used for `datasets/`.

## What's in the repo

* `cpp/`: the C++ source of the binary
* `python/`: the prototype the C++ output is checked against
* `datasets/`: daily, per-chain, per-pair and size-band totals, plus the validation results
* `demo/`: a static dashboard over a synthetic market (`demo/index.html`)
* `reports/`: charts of the datasets and of the sample scan (`reports/index.html`, PNGs in `reports/png/`)
* `samples/`: one three-day scan in full, every trade with its transaction ids, for reading without running anything
* `fixtures/` and `ci/`: real mainnet blocks and the checks CI runs on them
* `deploy/systemd/`: units for running it continuously
* `docs/`: detection method, validation, configuration, operations and privacy
* `CHANGELOG.md`: what changed in each release

Per-chain summary from `datasets/chains.csv`:

```
chain,completed,refunded,refund_pct,first_day,last_day,lock_to_spend_min_p10,lock_to_spend_min_p50,lock_to_spend_min_p90,refund_tx_pending
BTC,1515,97,6.02,2026-05-19,2026-11-19,9.8,19.4,56.6,0
LTC,785,61,7.21,2026-05-24,2026-11-19,10.1,15.2,26.0,0
PART,233,16,6.43,2026-05-14,2026-11-07,7.2,12.5,20.5,1
```

## How it's built

It's about 5,000 lines of C++20 on SQLite. It talks to Bitcoin and Litecoin peers directly and checks every block's proof of work and merkle root, so it needs no data provider. The
live watcher runs in 10 to 25 MB and under 1% of a core.

The Python prototype in `python/` is the reference. The C++ output has to match it byte for byte; before the switch to C++,
side-by-side runs on production data compared 1,232,693 rows with 0 differences. The parsers are fuzzed with libFuzzer and sanitizers.

## How accurate it is

| Measure | Result |
| --- | --- |
| Completed trades found, Bitcoin | 537 of 539 (99.6%, 95% CI 98.7-99.9) |
| Same, round amounts included | 96.1% (95% CI 94.2-97.4) |
| Completed trades found, Litecoin | 11 of 11 |
| Early flags that became trades, Bitcoin, 6 days | 219 of 226 (96.9%) |
| Trades flagged before settlement, Bitcoin, 6 days | 72%, a median 18.8 minutes ahead |

Measured on one deployment over a few months. Methods are in `docs/validation.md`.

## Limitations

* BasicSwap's transaction format, not BasicSwap only. Any client that builds the same transactions is counted: forks, and
BasicSwap's own swaps between two scripted coins (Bitcoin against Litecoin, for example). If a client
release changes its transaction format, detection breaks until the rules are updated.
* No Monero data. Direction, the other coin and the counter-amount come from the matched quote; an unmatched trade has none of them.
* The pricing figures in `docs/market-making.md` (premiums to Kraken, USD volume) come from the Python matcher with exchange prices
and recorded order books that are not shipped. The C++ `match` gives the pair, the quote's rate and the maker from your own snapshots.
* Counts are swaps, not traders: one actor repeating the same trade shows up many times (one day in August had 94 Litecoin swaps
with 10 distinct amounts).
* Bitcoin Cash trades settle through a covenant script; they are counted in `daily_swaps.csv` as `bch_covenant_swaps` (a refund
passes through a second covenant, so `bch_covenant_spends` can count one swap twice), not in the completed totals.
* Secret-hash (HTLC) swaps, which BasicSwap still offers for some pairs, use a script template shared with other atomic-swap
software, so they are not in the completed totals either: `secret_hash_redeems_any_protocol` counts redeems per chain (one swap
can appear on two chains), `secret_hash_swaps_two_chains` counts a swap once when both legs are on scanned chains.
* Refunds are counted once the refund transaction's output is spent; `chains.csv` `refund_tx_pending` shows the ones still waiting,
so the newest days undercount refunds. Pair and maker volume count completed swaps only.
* USD values are the locked (visible) leg at the Kraken mid of its coin at the lock time; `offer_matched_unpriced` counts matched
swaps without a price.
* Recall can only be checked on trades that fill a whole public quote: 537 of 539 on Bitcoin, 11 of 11 on Litecoin. Most
Litecoin trades are partial fills, so few qualify, and Particl and Bitcoin Cash have had only 8 trades each since I started
recording the order book.

## Privacy

The signals that make this work (a distinctive lock script, amounts that match quotes exactly, long-lived maker addresses) also
expose BasicSwap users; `docs/privacy.md` has the numbers. This repository has no order-book or maker data. Besides daily and
per-pair totals and a synthetic demo market, `samples/` holds one three-day scan trade by trade, which anyone can reproduce with
the commands in `docs/walkthrough.md`.

## Trying it

On Debian or Ubuntu:

```
sudo apt-get install -y git cmake g++ libsqlite3-dev zlib1g-dev libssl-dev python3
bash cpp/build.sh release                                     # about a minute
bash ci/run_checks.sh                                         # offline: self-test, real blocks against the Python reference, tests
bin/bsx-swap-tracker scan --coins BTC,LTC --probe             # first run syncs block headers, about 2 minutes
bin/bsx-swap-tracker scan --coins BTC,LTC --backfill-days 1   # the last day of trades, about 3 minutes
bin/bsx-swap-tracker report                                   # aggregates in data/analysis/chainscan/report/
```

`docs/walkthrough.md` goes through each step with the output to expect and what to look for in the data, including the live
watcher and matching against your own BasicSwap node. `ci/walkthrough.sh` runs the live steps in one go.

Detection rules: `docs/method.md`. Setup: `docs/configuration.md` and `docs/operations.md`.
