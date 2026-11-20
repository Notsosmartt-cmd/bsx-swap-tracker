# Changelog

## Unreleased

- Docs: `method.md` gains a table of the terms used in the docs and datasets; release notes moved out of `method.md` and
  `validation.md` into this file.

## 0.1.1 (2026-11-20)

- Offer matches are graded `unique`, `ambiguous` or `round`, and only unique matches count towards pairs, makers and USD volume.
  0.1.0 used the first matching offer, in an order that could change between runs.
- The lock side follows BasicSwap's rule (`is_reverse_ads_bid`). 0.1.0 tried both sides of an offer, so on a pair of two scripted
  coins (BTC/LTC) a lock could match the wrong side.
- Particl blind (confidential) swaps are recognised; Particl is in both matchers.
- Secret-hash swaps are counted once across two chains and kept out of the completed totals.
- Bitcoin Cash covenant swaps are counted (`bch_covenant_swaps`).
- Refunds are left out of pair and maker volume; USD comes from the locked leg, with a count of unpriced swaps.
- A refund transaction is upgraded to `certain` when its payout is stored, in either order, and downgraded on a reorg.
- Test fixtures grow to 24 blocks; datasets, demo and reports regenerated.

## 0.1.0 (2026-11-20)

First public release: `scan`, `watch`, `snapshot`, `match` and `report`; six months of daily datasets to 19 November 2026; one
sample scan; reports and a demo page.
