# Samples

The complete output of one scan, for reading without running anything. Each folder is one run.

## 2026-11-20

A fresh clone with an empty home directory and no history ran the commands in `docs/walkthrough.md` on 20 November 2026:
a one-day scan, then `--backfill-days 3`, a 10-minute `watch`, and `report`. It covers Bitcoin blocks 976334 to 976769 and
Litecoin blocks 3214698 to 3216426: Bitcoin from 22:30 UTC and Litecoin from 14:55 UTC on 17 November, both to about 18:30 UTC
on 20 November. The first and last days are partial.

| File | What's in it |
| --- | --- |
| `trades.csv` | every completed (160) and failed (5) trade: chain, settlement and lock times, minutes between them, amount, block height, the settlement and lock transaction ids |
| `event_counts.csv` | every event the scan recognised, by tier, and which ones `report` counts (the thousands of `candidate` rows are ordinary multisig spends) |
| `report/` | `daily_swaps.csv`, `chains.csv` and `sizes.csv` from `bsx-swap-tracker report` |
| `watch_status.json` | the live watcher's status after 10 minutes: transactions and blocks seen, parse errors, flags (0: no order book), reconnects |
| `scan_summary.txt` | blocks scanned per run and how long they took |

Three refunds have an empty amount: their lock transaction is older than the scan window, so the scan never saw the lock output.
Paste any transaction id into a Bitcoin or Litecoin block explorer to check it. For a completed trade, the settlement spends the
lock output with one input and one output, transaction version 2 and sequence 0, and its witness shows the 2-of-2 script.

The two full days agree with the shipped six-month dataset, which came from a long-running deployment: 18 November has 80
completed and 1 failed on Bitcoin and 13 and 0 on Litecoin in both, and 19 November has 23 and 2, 6 and 1. The quote-match
columns are 0 here because the run had no order-book snapshots, so the rows carry no pair, price or maker.

To make your own: run the walkthrough, then `python3 samples/make_sample.py --out samples/<name>`.
