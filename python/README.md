# Python reference and lab

This is the original proof of concept, kept as the reference implementation. Every rule is written and measured here before it is
ported to the C++ runtime in `cpp/`, and the C++ output must match it byte for byte (see `docs/validation.md`). Python 3.11 or newer.

## Runs on its own (standard library only)

| File | What |
|---|---|
| `python/collectors/chainscan_lib.py` | parsers and recognition (templates, tiers): the specification of `cpp/src/scan.cpp` |
| `python/collectors/chain_swap_scan.py` | scanner (the same database schema as the C++ runtime) |
| `python/collectors/p2p_blocks.py`, `python/collectors/part_p2p.py` | peer-to-peer block sources with header verification |
| `python/collectors/chain_watch.py` | live watcher (`--selftest` needs no network) |
| `python/collectors/collector_settings.py`, `python/collectors/collector_common.py` | settings file, logging, single-instance locks |
| `python/collectors/chain/cpp_corpus.py` | golden corpus: build, compare C++ against Python (`--corpus fixtures/corpus`) |
| `python/collectors/chain/cpp_fuzz_seeds.py` | fuzz seeds from the corpus (`BSX_CORPUS_DIR=fixtures/corpus`) |
| `python/collectors/chain/cpp_shadow_compare.py` | table-by-table compare of a C++ shadow database against the Python one |
| `python/collectors/tests/` | unit tests (`python3 python/collectors/tests/test_chainscan.py`, ...) |

## Measurement tools: they need inputs this repository does not ship

These produced the figures in `datasets/validation.json`. They read board snapshots converted to Parquet tables, and for USD
values an exchange price history. Neither is published (see `docs/privacy.md`), so they show the method rather than run
out of the box. They need `pip install -r python/requirements-lab.txt`.

| File | Measures | Needs |
|---|---|---|
| `python/collectors/chain/chain_recall.py` | recall of completed swaps against offer-matched locks, with a shifted-offer null | scan database, board offers (Parquet) |
| `python/collectors/chain/chain_lock_early_detect.py` | early-detection precision, recall and warning time | scan database, board offers (Parquet) |
| `python/collectors/chain/chain_swap_report.py` | events per day, secret-hash swap legs, attribution, cross-checks | scan database, board offers, prices |
| `python/collectors/offer_match_live.py`, `python/collectors/trap_labels.py` | offer match with prices, refund (trap) labels | board offers (Parquet and raw snapshots), prices |
| `python/tools/chain_swap_export.py` | the data behind the dashboard tab | scan database |

`bsx-swap-tracker match` (C++) needs only the scan database and snapshot files, and covers the offer match without prices.
