# Privacy: what this measures, and how it could be fixed

Atomic swaps are often described as private. On BasicSwap, the scripted leg of an adaptor swap is recognisable on chain, and the
public order book tells an observer what to look for. This document describes the leak this tool measures and the fixes that
would close it.

## Leak surfaces

1. **Lock script and transaction shape.** A 71-byte 2-of-2 script with unsorted keys, spent by a version-2 transaction with one
   input, one output and nSequence 0, from a lock created with nLockTime 0. Each feature alone is common; together they are
   distinctive enough for 99.6 % recall in the strong tier.
2. **Exact amounts.** The lock amount equals the offer amount to the satoshi. A public offer plus an on-chain output with the same
   odd amount, at the same time, links the two.
3. **Offer addresses.** Offers are signed by a network address that a maker usually keeps. Matching swaps to offers builds a
   per-maker trading record.
4. **Refund path.** The lock-refund script is unique to the protocol. A refund identifies itself as a failed BasicSwap swap.
5. **Aggregation.** Over months, these combine into volume, size habits and counterparty timing per maker. Pseudonymity covers the
   person, not the trading record.

## How much leaks

Measured on mainnet by this tracker:

- completed swaps on BTC: 99.6 % found;
- matched to the offer and maker: 63 % of completed BTC trades since 10 October (exactly one quote, non-round amount);
- early detection while a swap is in progress: 96.9 % precision.

## What would make swaps harder to spot

| Change in BasicSwap | Effect | Constraint |
|---|---|---|
| Lock transaction nLockTime = current chain height (like ordinary wallets) | removes the nLockTime-0 feature | verifiers already accept up to chain height + 2, so old peers stay compatible |
| Wallet-default fields on the spend (nSequence and nLockTime) | the spend looks like an ordinary wallet's | the spend-side lock time must be capped by the verifier, or a counterparty could delay the redeem past the refund timelock |
| Sorted keys in the 2-of-2 script | removes the key-order signature (and makes it look like Lightning funding) | every path that signs the 2-of-2 (spend, pre-refund, refund) must follow the sorted order; gate on the offer's protocol version |
| Taproot key-path locks (MuSig2) on chains that support it | a lock and its cooperative spend look like any single-key payment | new cryptography (nonce handling) |
| Amount hiding (offer amounts rounded or split, change on the lock) | weakens the amount match | changes offer semantics; needs maker and taker agreement |

With the first three changes, script-based detection stops working on updated clients. A detector is left with amount matching,
measured at about 34 % precision and 82 % recall on one day of BTC blocks.

## What this repository publishes

No board snapshots, no maker addresses and no offer-match files, so nothing here ties a trade to a quote, a price or a maker.
The six months in `datasets/` are daily counts per chain and the validation figures. The one exception to "aggregates only" is
`samples/`: the complete output of one short scan (three days in November 2026), with a row and transaction ids per trade, so
readers can check the method against a block explorer without running it. That is public chain data that anyone gets from the
commands in `walkthrough.md` in a few minutes, and without order-book snapshots it carries no pair, price or maker. USD volume is rounded to 100 USD and left out on days with fewer than 3 matched swaps
on a chain. The demo market is synthetic. Anyone running the tool collects their own data from public chains and their own node.
