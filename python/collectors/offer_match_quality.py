"""Match quality of lock -> public-offer matches (2026-11-20; docs/method.md).

A lock is matched to every live offer whose full amount on that coin equals the lock value (+-2 sat).  Two weaknesses found in the review:
  - several candidate offers for one lock (77 of 652 matched events on 2026-11-20; 65 of them with different makers, 27 with different pairs):
    which one is "the" offer is unknown, so pair, maker and USD of such a lock are not attributable;
  - round lock amounts (a multiple of 1,000 sat) match offers by chance far more often (section 10: 82 of 91 round LTC candidate matches were
    chance), which is why the recall check (python/collectors/chain/chain_recall.py) already uses non-round amounts only.
Every match row gets three columns; reports count only match_quality == "unique" towards pairs, makers and USD (the other rows stay in the file):
  n_candidates   distinct offers (msg_id) matched by the same lock txid and outcome
  round_amount   1 if the lock amount is a multiple of 1,000 base units, else 0
  match_quality  "unique" (one candidate, non-round), "ambiguous" (more than one candidate), "round" (one candidate, round amount)
Rows are sorted by (lock_time, txid, first_seen, msg_id) so the file no longer depends on a join's row order."""

QUALITY_FIELDS = ["n_candidates", "round_amount", "match_quality"]
ROUND_UNIT = 1000

# Which side of an offer holds the script lock (2026-11-20, review item 10): BasicSwap `is_reverse_ads_bid(coin_from, coin_to)` is true when
# coin_from is scriptless or has no segwit; then the lock is on coin_to and the taker locks first ("reverse"), otherwise on coin_from
# ("normal").  A lock on coin X therefore matches an offer's amount_to only when the offer is reverse (coin_from in REVERSE_COINS) and its
# amount_from only when it is not.  Same set as python/collectors/chain/alock_offer_match.py.
REVERSE_COINS = frozenset({"XMR", "WOW", "PART_ANON", "FIRO", "DOGE", "DASH", "PIVX"})
# Kraken pair -> coin, for USD values (the ticker table has no PART or WOW)
PRICE_PAIRS = {"XBTUSD": "BTC", "XMRUSD": "XMR", "LTCUSD": "LTC", "BCHUSD": "BCH", "XDGUSD": "DOGE", "DASHUSD": "DASH"}


def lock_direction(lock_coin, cf, ct):
    """'normal' / 'reverse' when an offer cf->ct can hold its script lock on lock_coin, else None"""
    if cf in REVERSE_COINS:
        return "reverse" if ct == lock_coin and ct not in REVERSE_COINS else None
    return "normal" if cf == lock_coin else None


def usd_value(lock_coin, lock_sat, price, amt_from=None, cf=None, amt_to=None, ct=None):
    """USD value of a matched swap (review item 7): the locked, visible leg at the Kraken mid of its own coin at the lock time; when that
    coin has no Kraken price (PART), the offer's other legs.  price(coin) -> USD mid or None."""
    p = price(lock_coin)
    if p and lock_sat is not None:
        return round(float(lock_sat) / 1e8 * p, 2)
    for amt, coin in ((amt_to, ct), (amt_from, cf)):
        p = price(coin) if coin else None
        if p and amt is not None:
            try:
                return round(float(amt) * p, 2)
            except (TypeError, ValueError):
                continue
    return None


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def quality(n_candidates, lock_sat):
    rnd = lock_sat is not None and lock_sat % ROUND_UNIT == 0
    if n_candidates > 1:
        return "ambiguous", rnd
    return ("round" if rnd else "unique"), rnd


def annotate(rows):
    """add QUALITY_FIELDS to every row (dicts; values may be numbers or CSV strings) and return them in a deterministic order"""
    cands = {}
    for r in rows:
        cands.setdefault((r.get("txid"), r.get("outcome")), set()).add(r.get("msg_id"))
    for r in rows:
        n = len(cands[(r.get("txid"), r.get("outcome"))])
        q, rnd = quality(n, _num(r.get("lock_sat")))
        r["n_candidates"], r["round_amount"], r["match_quality"] = n, int(rnd), q
    rows.sort(key=lambda r: (_num(r.get("lock_time")) or 0, str(r.get("txid")), _num(r.get("first_seen")) or 0, str(r.get("msg_id"))))
    return rows


def summary(rows):
    """events (txid, outcome) by match quality"""
    seen = {}
    for r in rows:
        seen[(r.get("txid"), r.get("outcome"))] = r.get("match_quality")
    out = {}
    for q in seen.values():
        out[q] = out.get(q, 0) + 1
    return out
