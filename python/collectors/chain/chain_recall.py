"""Recall of the completed-swap detector per chain (2026-11-20; docs/method.md), against an independent ground truth: locks whose amount equals a live public offer's
full amount (+-2 sat, offer seen within [lock-3600, lock+600] as trap_labels), non-round, spent through BasicSwap's 71-byte 2-of-2.
Null model: the same matching with offers shifted by +-1 day estimates chance matches.
Run (reads the scan database; heavy on a large one, so at idle priority):
  nice -n 19 ionice -c3 python3 python/collectors/chain/chain_recall.py BTC,LTC"""
import bisect, collections, math, sqlite3, sys, time
import duckdb
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
T0 = 1791604800   # 2026-10-10, first board snapshot
c = duckdb.connect(); c.execute("set memory_limit='400MB'; set threads=1")
off = c.execute(f"""select coin_from, coin_to, amount_from, amount_to, epoch(first_seen)::bigint, epoch(last_seen)::bigint
                    from read_parquet('{ROOT}/data/parquet/offer_lifecycle_site/**/*.parquet')""").fetchall()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector_settings import S as SETTINGS  # noqa: E402  data folder of the scan database (settings file or the default layout)
db = sqlite3.connect(f"file:{os.path.join(SETTINGS.data_dir, 'chainscan.sqlite')}?mode=ro", uri=True, timeout=60)
def wilson(k, n, z=1.96):
    if n == 0: return (float("nan"), float("nan"))
    p = k / n; d = 1 + z*z/n; ce = (p + z*z/(2*n)) / d; m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (100*(ce-m), 100*(ce+m))
for coin in sys.argv[1].split(","):
    idx = sorted((a, f, l) for cf, ct, af, at, f, l in off for a in ((af,) if cf == coin else ()) + ((at,) if ct == coin else ()) if a)
    amts = [x[0] for x in idx]
    def n_match(v, t, shift=0):
        i = bisect.bisect_left(amts, v - 2); n = 0
        while i < len(idx) and idx[i][0] <= v + 2:
            a, f, l = idx[i]
            if f + shift <= t + 600 and l + shift >= t - 3600: n += 1
            i += 1
        return n
    kinds = ("adaptor_lock_spend", "adaptor_refund_tx") if coin != "BCH" else ("bch_covenant_spend",)
    rows = db.execute(f"""select kind, confidence, amount_sat, lock_time, block_time from swap_events where coin=? and block_time >= ?
                         and kind in ({",".join("?" * len(kinds))})""", (coin, T0, *kinds)).fetchall()
    st = collections.Counter(); nul = collections.Counter()
    no_lock = 0
    for kind, conf, amt, lt, bt in rows:
        if amt is None or lt is None:
            no_lock += 1; continue
        if lt < T0: continue
        nr = amt % 1000 != 0
        m = n_match(amt, lt)
        if m and nr: st[(kind, conf)] += 1
        for sh in (-86400, 86400):
            if nr and n_match(amt, lt, sh): nul[(kind, conf)] += 0.5
    ls = {k[1]: v for k, v in st.items() if k[0] == kinds[0]}
    lsn = {k[1]: v for k, v in nul.items() if k[0] == kinds[0]}
    tot = sum(ls.values()); strong = ls.get("strong", 0) + ls.get("certain", 0)
    nonstrong_true = sum(max(0, ls.get(t, 0) - lsn.get(t, 0)) for t in ("likely", "candidate"))
    lo, hi = wilson(strong, strong + round(nonstrong_true))
    print(f"{coin}: events since 10 Oct {len(rows)} (no lock data {no_lock}); amount-matched non-round lock spends by tier {dict(ls)}; "
          f"chance matches (offers shifted 1 day) {dict(lsn)}; refund tx matched {dict({k[1]: v for k, v in st.items() if k[0] != kinds[0]})}")
    print(f"   -> recall of the top tier among offer-matched locks: {strong} / {strong + round(nonstrong_true)} = "
          f"{100 * strong / max(1, strong + round(nonstrong_true)):.1f} % (95 % CI {lo:.1f}-{hi:.1f}); raw (no chance correction) {100 * strong / max(1, tot):.1f} %")
