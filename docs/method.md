# Method: how a swap is recognised on chain

BasicSwap settles a trade directly on two chains. On almost every pair it uses an adaptor-signature swap: the scripted coin
(BTC, LTC, BCH, PART, ...) is locked in a 2-of-2 output, and the other leg (usually Monero-family, sometimes a second scripted coin
or a coin added by a fork) is not linked to it on chain. The
tracker therefore watches the scripted chain. It reads every block, finds spends that reveal a script, and compares the script
with exact templates.

## 1. What is read

Each non-coinbase input that spends a script-hash output (P2SH or P2WSH), and whose revealed script is 34 to 1000 bytes long, is
stored in `script_spends`. For P2SH the revealed script is the last scriptSig push; for P2WSH it is the last witness item. Each
script is then matched against four templates, in this order.

| # | Template | Shape | Event kinds |
|---|---|---|---|
| 1 | Secret-hash contract (`atomic_swap_1`, 90-100 bytes) | `IF SIZE 32 EQUALVERIFY SHA256 <h> EQUALVERIFY DUP HASH160 <pkh> ELSE <t> CSV/CLTV DROP DUP HASH160 <pkh> ENDIF EQUALVERIFY CHECKSIG` | `htlc_redeem` (a 32-byte stack item hashes to `h`; the secret is stored), `htlc_refund` |
| 2 | Adaptor lock-refund script (114-125 bytes since BasicSwap 0.18; 111-122 bytes before) | `IF 1 CSV DROP 2 <pk> <pk> 2 CHECKMULTISIG ELSE <n> CSV DROP <pk> CHECKSIG ENDIF`; before 0.18 the IF branch had no `1 CSV DROP` | `adaptor_refund_spend` (branch stored; `"form": "pre_0_18"` for the older script) |
| 3 | Adaptor lock script (exactly 71 bytes) | `2 <pk33> <pk33> 2 CHECKMULTISIG` | `adaptor_refund_tx`, `adaptor_lock_spend` |
| 4 | BCH covenant (BCH only) | starts `TXINPUTCOUNT 1 NUMEQUALVERIFY TXOUTPUTCOUNT 1 NUMEQUALVERIFY`, uses introspection and `CSV DROP` | `bch_covenant_spend` |

The secret-hash contract is a standard shared by other atomic-swap software. A `certain` redeem proves the contract, not that
BasicSwap made it. `datasets/daily_swaps.csv` therefore labels that column "any protocol".

## 2. Tiers for a spend of the 2-of-2 lock

A plain 2-of-2 multisig is common: Lightning channel funding outputs have exactly the 71-byte shape. The transaction around the
spend decides the tier. Let:

- `plain` mean 1 input, 1 output, an output of type P2PKH or P2WPKH, and transaction version 2 (0xA0 on Particl); on Particl a
  blind (confidential) swap also counts as `plain`: 1 input, version 0xA0, exactly one data output (the fee) and one or two blinded
  outputs to P2PKH or P2WPKH, values hidden (BasicSwap `PARTInterfaceBlind`; recognised since 2026-11-20, amount unknown);
- `seq0` mean the input's nSequence is 0;
- `lock_lt` mean the nLockTime of the transaction that created the 2-of-2 output, when that transaction is in the index.

| Tier | Rule | Measured meaning |
|---|---|---|
| strong | `plain` and `seq0` and `lock_lt` is 0 or unknown | real swaps (recall 99.6 % on BTC, see validation) |
| likely | `plain` and `lock_lt` is 0 or unknown, nSequence not 0 | indistinguishable from chance when matched against offers: treat as candidate |
| candidate | any other spend of a 71-byte 2-of-2 | almost all Lightning and other multisig |

A spend with one input, one P2WSH or P2SH output, and a relative-lock nSequence is an `adaptor_refund_tx`, at tier `likely` (on
Particl blind swaps: one data output and blinded outputs of which exactly one is P2WSH or P2SH). It becomes `certain` once its
output is spent through template 2.

**Key order check.** Lightning sorts the two keys (BOLT 3); BasicSwap writes leader then follower, unsorted. In a tier that holds
only BasicSwap locks, the first key is the greater in about half the cases; in a tier that holds Lightning, almost never. The
strong tier passes this test.

**Tested and not adopted (2026-11-20).** Some 2-of-2 spends have every strong feature except a lock nLockTime 0 to 100 blocks
below the lock's own height, the shape of a client that sets the lock time to the chain height. Accepting them would add 46
spends. Of the 35 since board data begins, none matched a public offer; at the strong tier's matching rate about 3.3 should have
(chance of none: 1.6 %). They stay `candidate`. A unit test pins this decision.

## 3. Verifying what is read

- **BTC and LTC:** blocks come from public peers. Each block's hash must be in the header chain, every header links to the one
  before it and meets its own proof of work (scrypt for LTC), and the merkle root is recomputed from the transactions. Headers come
  from BasicSwap's Electrum header files when present; otherwise the tracker syncs them from a built-in checkpoint. Every synced
  header must link, meet its proof of work, and have a target no easier than four times the checkpoint's, so a peer cannot feed a
  cheap fake chain.
- **PART:** blocks come from the local Particl node's peer port, with witness data. The parser handles typed outputs (standard,
  CT, RingCT, data) and anon inputs.
- **BCH:** blocks come from the local node's RPC, restricted to an allowlist of four read-only methods with fixed parameter shapes,
  on loopback only, and never on a protected port.

## 4. Offer matching (attribution)

A completed swap (strong lock spend) or refund (certain refund transaction) has a coin, an amount and a time. It is matched to
every public offer that meets all three conditions:

- its full amount on that coin equals the lock amount within 2 satoshi;
- it was first seen no later than 600 s after the lock;
- it was last seen no earlier than 3600 s before the lock.

Each match row records `n_candidates` (how many offers the lock matched), `round_amount` (the lock is a multiple of 1,000 base
units) and `match_quality`: `unique` (one candidate, non-round), `ambiguous` (more than one candidate: which offer was filled is
unknown) or `round` (one candidate, round amount: mostly chance; on Litecoin 82 of 91 such matches were). Only `unique` matches
count towards pairs, makers and USD; `daily_swaps.csv` counts the other two in `offer_matched_ambiguous` and
`offer_matched_round_amount`. Until 2026-11-20 the first matching row was used, in an order that could change between runs.

The offer gives the maker address, the direction, the rate and the terms. `same_terms_24h` counts offers with identical terms
created in the 24 h before the matched one, a measure of how often a maker reposts. Offers come from board snapshots: either the
public markets site's format, or your own node through `bsx-swap-tracker snapshot`.

## 5. Early detection (live watcher)

The watcher keeps a peer connection per chain, sees each unconfirmed transaction and each new block, and flags an output as a
probable swap lock when all of these hold:

1. it is a P2WSH output (P2SH32 on BCH);
2. its amount equals a live offer's amount within 2 satoshi, using offers from the snapshots of the last 2 hours;
3. the transaction has version 2 (0xA0 on Particl) and nLockTime 0;
4. the transaction has at most 2 outputs;
5. the amount is not a multiple of 1000 satoshi.

The swap is then still in progress. The flag arrives a median 18.8 minutes before the spend that completes it. The watcher also
starts a scan 45 s and 180 s after each block, so completed swaps reach the database within minutes.

## 5b. Counting rules (2026-11-20)

- **Lock side.** BasicSwap puts the script lock on coin_from unless coin_from is scriptless or has no segwit (`is_reverse_ads_bid`:
  XMR, WOW, PART_ANON, FIRO, DOGE, DASH, PIVX), in which case the lock is on coin_to and the taker locks first (`reverse`). A lock
  on coin X is matched to an offer's amount_from only for a normal offer with coin_from = X, and to its amount_to only for a reverse
  offer with coin_to = X. (Before, both sides were tried; on scripted-coin pairs such as BTC/LTC an LTC lock could match the wrong side.)
- **USD.** The locked leg at the Kraken mid of its own coin at the lock time; if that coin has no Kraken price (PART), the offer's
  other leg. `offer_matched_unpriced` counts uniquely matched completed swaps with no price.
- **Refund tier.** A refund transaction is `certain` while a spend of its output through the refund script is stored: it is upgraded
  when the spend arrives, also when the spend was stored first (an older range scanned after the tip), and turned back to `likely`
  when a reorg removes the spend. Refunds count once certain; `refund_tx_pending` in `chains.csv` shows the likely ones.
- **Volume.** Pair and maker volume and median lock sizes count completed swaps; refunds are counted separately.
- **BCH covenant.** A covenant spend whose input was created by another covenant spend is the second step of a refund, so
  `bch_covenant_swaps` counts covenant spends whose input was not; `bch_covenant_spends` counts every spend.
- **Secret-hash swaps.** The HTLC template is shared with other software, so these are kept out of the completed totals. A secret
  hash redeemed on two scanned chains is one swap, counted once on the day and chain of its first redeem (`secret_hash_swaps_two_chains`).
- **Python and C++.** The C++ runtime reads raw blocks, which carry no input values, so a lock spend's amount comes from the
  script-hash output index only; the Python reference also uses an input value when an RPC source supplies one (verbose JSON
  blocks), which can fill the amount of a lock older than the index window. On the chains scanned in production (raw blocks for BTC,
  LTC, PART; BCH has no adaptor locks) the two agree. The C++ `match` command looks back 2 days by default (`--days`) and leaves USD
  empty; the published datasets use the Python matcher's file, which has USD.

## 6. Limits

- The Monero leg is invisible, so direction, the other coin and the counter-amount come from the matched offer, or are unknown.
  Swaps whose other leg is not Monero (BasicSwap scripted-coin pairs, forks) are counted the same way.
- A strong spend whose lock transaction is older than the index window passes the lock check by default.
- Recall is measured where offers can be matched (from 2026-10-10). Precision of the strong tier is measured indirectly: offer
  matches against a shifted-offer null, and the key-order test.
- A client that changes its transaction fields (see `privacy.md`) moves out of the strong tier. Detection then falls back to
  amount matching, which is much weaker.
