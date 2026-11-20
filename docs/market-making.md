# The market-making use case

The tracker was built for market making on BasicSwap. A maker on BasicSwap quotes blind: the board shows offers, never
trades. These are the questions the tracker answers, and how. The views are shown on `demo/index.html` with synthetic data.

| Question | Data | How |
|---|---|---|
| How much really trades, per pair and per day? | completed swaps (strong) and refunds (certain), offer-matched for pair and size | `scan` + `match` + `report` (daily, per chain, per pair, size bands, makers); the shipped `datasets/` are `report --public` |
| Which sizes fill, at what premium? | matched offers against all offers seen on the board | fill rate per size bucket and premium band (demo: "Fill rate by offer size") |
| Who is the competition? | maker address of matched offers | share of matched volume per maker; reposting rate (`same_terms_24h`) |
| Which offer families end in refunds? | certain refund transactions matched to offers | trap labels: refund share per maker and terms; avoid quoting into them |
| Is someone filling a quote right now? | early lock flags (lock seen in mempool or block, amount equals one live offer) | a median 18.8 minutes of warning before completion; hedge or reprice before the swap settles |
| How long is capital locked? | lock to spend time per swap | p10 / p50 / p90 per pair; feeds the inventory and hedge-horizon model |

## Hedging with early warnings

When a flag appears on one of your own offers, you know the counterparty has locked before the swap completes. A desk can open the
hedge (for example a futures position on the scripted coin) at the lock rather than at completion, which shortens the window of
unhedged price exposure by the median warning time. For flags on other makers' offers, the stream is a live signal of
where volume is going.

## Replay and simulation

Completed swaps with matched offers form a fill history: an offer at this size and this premium was taken after this long. A
simulator steps a quoting strategy through the board history and fills it where real swaps happened. It can compare strategies
hedged and unhedged, before any capital is committed. The synthetic market in `datasets/synthetic/` has the same shape (makers,
offers, swaps, early flags) and is a safe test input for such tools.

## Measured so far

Figures from quote-matched trades since 10 October 2026 (when order-book recording began), priced against Kraken. They cover only
matched trades: those that fit exactly one quote with a non-round amount (63% of completed Bitcoin trades since 10 October), on one
deployment.

Pricing (uniquely matched swaps only). Takers buying XMR paid a median 3.1% over Kraken (484 swaps), on quotes that had been up a
median 20 minutes; takers selling XMR received 1.1% more than Kraken would have paid (42 swaps). These counts are the Python matcher's priced
swaps; `pairs.csv`, built by the C++ report under the public rules, counts 478 and 41. Monero is rich on this venue in both directions, and that gap is the spread makers
compete for. Fill probability against premium, size and quote age is the input an Avellaneda-Stoikov style quoting model needs:
matched fills give the numerator and the quotes that expired untouched (in the snapshots) give the denominator.

Adverse selection. Comparing each fill's premium when the quote was posted with its premium at fill, the median drift is 0.02
percentage points and the middle half sits within about a quarter of a point either way, so the typical fill is not a stale-quote
pick-off. The median says nothing about the tail, though: the proper measure is the markout, how far the reference price moves
after each fill, which the matched trades allow but which is not computed here. The swap protocol adds a risk exchanges don't have: whoever locks second can walk away if the price moves during the
swap. That option is worth about 9-19 bp of notional (an at-the-money option is worth about 0.4 standard deviations of
the price move over its life, and XMR/LTC moves 0.23-0.48% in the 14.5 minutes between a bid and the second lock), although stock BasicSwap software never exercises it. Since May 6.0% of
Bitcoin trades (97 of 1,612) and 7.2% of Litecoin trades (61 of 846) ended in a refund. On Bitcoin the rate fell from 13% in May to
August to 3% since September, which coincides with BasicSwap 0.18 changing the refund path (its release notes, 24 August 2026); the data cannot say whether
the release caused the drop.

Hedging. For quote-matched trades, funds stay committed a median 18 minutes from lock to settlement (45 at the 90th percentile;
19 and 57 for all Bitcoin trades in `chains.csv`). Hedging USD 1k of XMR/USD on Kraken costs about 5-6 bp, measured by sweeping
recorded order books (10-18 bp at the 90th percentile). An early flag on your own quote moves the hedge to the counterparty's lock, which removes most of that
window.

Competition. One maker address carried about a third of matched volume, and makers repost the same terms a median 21 times in the
24 hours before a fill (a bot re-pricing on a timer). Some bots use a new address per quote, so per-address shares understate
concentration.

## What it does not tell you

- The Monero leg: direction and counter-amount come only from the matched offer.
- Swaps on private or unlisted offers: no offer match, so pair and price are unknown, though the lock spend is still counted.
- Prices: the C++ matcher leaves the price columns empty. The Python `offer_match_live.py` adds them from an exchange ticker if you
  provide one.
