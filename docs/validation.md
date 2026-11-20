# Validation

All figures are in `datasets/validation.json`, regenerated with each release. They were measured on mainnet by the author's
deployment, with data up to 20 November 2026. Confidence intervals are 95 % Wilson intervals. What changed between releases is in
`CHANGELOG.md`.

## Recall of completed swaps

**Question:** of the swaps we know happened, how many does the top tier find?

**Ground truth:** locks whose amount matches a public offer exactly (2 satoshi tolerance, offer live at lock time) and is not a
multiple of 1000 satoshi.

**Chance correction:** the same match is repeated with the offers shifted by one day, and those chance matches are subtracted.

| Chain | Result |
|---|---|
| BTC | 537 of 539, 99.6 % (98.7-99.9) |
| LTC | 11 of 11 (74.1-100) |
| PART | no offer-matched swaps since board data begins; structurally, 233 of 235 BasicSwap-shaped spends reach the top tier (99.1 %), including 10 blind (confidential) swaps |
| BCH | covenant swaps are recognised by template (certain tier); not measurable by offer match |

Why the samples outside Bitcoin are small (top-tier events since 10 October 2026): every quote on the board allows
partial fills, and only a trade that fills a whole quote has a locked amount equal to the quote. On Bitcoin 586 of 827 events match
a full quote exactly; on Litecoin 36 of 198 (142 fall inside a quote's bid range, i.e. partial fills); Particl and Bitcoin Cash had
8 top-tier events each. Including round amounts gives more cases but the chance correction then dominates (Bitcoin 96.1 %,
95 % CI 94.2-97.4; Litecoin 30 of 39, CI 62-87; Particl 3 matches, too few), so the published figures use non-round amounts only.

The two BTC misses, and the two PART misses, are spends whose lock transaction has a non-zero nLockTime. A rule that accepted
those was tested and not adopted (see `method.md` section 2).

## Precision

- **Offer matches:** since 10 October, 510 of 807 completed BTC trades (63 %) match exactly one public quote with a non-round
  amount (`offer_matched` in `daily_swaps.csv`; 592, or 73 %, counting ambiguous and round-amount matches too). With the quotes
  shifted by a day, top-tier spends match 3-4 % of the time. The `likely` and `candidate` tiers match at chance level.
- **Key order:** in the strong tier the first key is the greater about half the time, which is the BasicSwap signature. Lightning
  would give zero. At most about 2.5 % of strong BTC events could be Lightning.

## Early detection

Over 6.2 days of BTC blocks (1,008 blocks, 11 to 17 November 2026), with the five rules of `method.md` section 5, 219 of 226
flags older than 6 hours later completed or refunded as swaps: precision 96.9 % (93.7-98.5). Recall at lock time is 72 %.

Warning time before completion, from the lock to the spend:

| p10 | median | p90 |
|---|---|---|
| 9.9 min | 18.8 min | 50.3 min |

Matching on the amount alone, without the transaction-shape rules, is weak. Precision was 1.2 %, 25.6 % and 34.4 % at recall
91 %, 82 % and 82 % for three rule sets on 144 blocks. This matters for `privacy.md`: once transaction shapes stop being
distinctive, amount matching is all a detector has left.

## Refunds

A failed trade shows up as a refund transaction, and it is counted once the refund is paid out through BasicSwap's lock-refund
script (template 2 in `method.md`). BasicSwap 0.18 (24 August 2026) changed that script: the cooperative branch gained a one-block
relative lock (`IF 1 CSV DROP 2 <pk> <pk> 2 CHECKMULTISIG ELSE ...`). Both forms are recognised; spends of the older 115-byte form
carry `"form": "pre_0_18"`. The cut in the data is sharp: all 109 refunds before late August were paid out through the older
form (with the timeout key equal to the second multisig key, as in the new one), all later ones through the new form.

| Months | Bitcoin | Litecoin | Particl |
| --- | --- | --- | --- |
| May to August | 64 of 480 (13.3%, 95% CI 10.6-16.7) | 40 of 510 (7.8%, 5.8-10.5) | 10 of 207 (4.8%) |
| September to 19 November | 33 of 1,132 (2.9%, 2.1-4.1) | 21 of 336 (6.2%, 4.1-9.4) | 6 of 32 |
| November to the 19th | 6 of 591 (1.0%) | 5 of 105 (4.8%) | 0 of 3 |

Counts are failed trades out of all trades (completed plus failed). The Bitcoin drop lines up with 0.18's refund-path
hardening. Two checks on the earlier months, where no order book was recorded to measure recall: the completed-trade rule depends
on fields BasicSwap sets identically on Bitcoin and Litecoin, and on Litecoin, which has little other multisig traffic, nearly
every BasicSwap-shaped spend reached the top tier in every month (0 to 5 lower-tier spends a month), so the completed counts are
not short before 0.18. Among trades that matched a public quote exactly, 17 of 652 failed (2.6%) since 10 October, between the
chain-wide rates for that period: 2.1% on Bitcoin (17 of 824) and 3.2% on all chains (33 of 1,030).

## C++ against the Python reference

- **Golden corpus:** 35 public blocks across the four chains. The C++ parser dump and recognised events are byte-identical to
  Python. CI repeats this on `fixtures/` (24 blocks).
- **Shadow runs:** the C++ scanner and watcher run next to the Python ones, into a separate database. A daily compare checks every
  table over the overlapping heights. See `cpp_vs_python.shadow_runs` in `validation.json` for the totals; 0 rows differed.
- **Switch to C++:** the C++ runtime became the default on 20 November 2026. Over the shadow runs from 19 November 23:14 to
  20 November 19:24 UTC, 64 table comparisons covered 1,232,693 rows with 0 differing.

## Fuzzing

There are five libFuzzer targets (blocks, transactions, scripts, JSON, P2P framing), built with ASan and UBSan. One bug was found
and fixed: the JSON writer escaped non-ASCII per byte instead of per code point. A regression self-test covers it. There are no
open crashes.

## Tests in this repository

| Test | Command |
|---|---|
| C++ self-test (hashes, varints, output types, JSON equal to Python, tier rules) | `bin/bsx-swap-tracker selftest` |
| Corpus parity | `python3 python/collectors/chain/cpp_corpus.py --corpus fixtures/corpus --compare bin/bsx-swap-tracker --events` |
| Python unit tests | `python3 python/collectors/tests/test_chainscan.py` (and `test_p2p_blocks.py`, `test_collector_settings.py`) |
| Chain-watch self-test | `python3 python/collectors/chain_watch.py --selftest` |
| Fixture integrity | `cd fixtures && sha256sum -c SHA256SUMS` |

`ci/run_checks.sh` runs all of these. GitHub Actions runs the same script.

## Self-test against the author's own swaps

Every BasicSwap database on the author's host that holds the author's own bids gives four Litecoin swaps (July and September 2026:
three completed, one refunded; two of them against coins added by a BasicSwap fork, not Monero). All four are found from the chain
alone with the exact locked amount: the three completed ones in the top tier, the refund as a certain refund transaction and refund
spend, not as a completed trade. None of them can test offer matching (order-book recording began later). Four swaps on one chain
are a sanity check, not a measurement.

## Offer-match quality

A lock can fit several quotes. Since 10 October 77 of 652 matched locks fitted more than one quote (most with different makers) and
32 more fitted one quote with a round amount, which on Litecoin is mostly chance. Both are kept in the match file with
`match_quality` `ambiguous` or `round` and left out of pair, maker and USD figures (`method.md` section 4).
