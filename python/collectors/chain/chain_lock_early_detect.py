"""Feasibility probe: can a BasicSwap swap be recognised when its lock is MINED instead of when it is SPENT?  (docs/method.md, section 14)

The chain swap scan recognises an adaptor-signature swap only when the 2-of-2 lock is spent, because the script is hidden until then.  At funding
time the lock is an ordinary P2WSH output.  But its value equals the amount of a public offer.  This probe takes every P2WSH output the scan indexed
on BTC in the last days (the index keeps 7 days) and asks: does its value equal the BTC amount of an offer that was live when it was mined (+-2 sat)?
Then it looks up what each matched output turned out to be (spent as a strong swap lock, a refund, another 2-of-2, something else, or still unspent)
and what share of the strong swaps of the window would have been caught at lock time.  A null (the same outputs against the offers of 3 days earlier)
gives the chance rate.

Read-only: reads <data_dir>/chainscan.sqlite (mode=ro) and data/parquet/offer_lifecycle_site; writes <data_dir>/report_lock_early_detect.json.
Run:  python3 python/collectors/chain/chain_lock_early_detect.py
      (the whole 7-day index: about 1 GB of database reads, a heavy job).  --blocks 300 reads only the newest 300 blocks (about 2 days, a light job).
"""
import argparse, bisect, json, os, sqlite3, sys, time
import duckdb
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
DB = os.path.join(ROOT, "data", "analysis", "chainscan", "chainscan.sqlite")
LIFE = os.path.join(ROOT, "data", "parquet", "offer_lifecycle_site")
OUT = os.path.join(ROOT, "data", "analysis", "chainscan", "report_lock_early_detect.json")
TOL_SAT = 2
BEFORE_S = 600            # an output may be mined up to this long before the offer's recorded creation time (clock differences)
AFTER_S = 5400            # a bid comes inside the offer's validity; allow this much more for acceptance and mining
SETTLE_S = 6 * 3600       # outputs younger than this are not counted in the precision (their swap may still be running)


def match(values, times, amt_sorted, created_sorted, valid_sorted, shift=0):
    """For each output (value, mined time) the number of offers whose BTC amount is within TOL_SAT and that were live around the mined time.
    amt_sorted is ascending; created_sorted and valid_sorted are in the same order.  shift moves the offers in time (the null)."""
    n = np.zeros(len(values), dtype=np.int32)
    start = np.asarray(created_sorted, dtype=np.int64) + shift - BEFORE_S
    end = np.asarray(created_sorted, dtype=np.int64) + shift + np.asarray(valid_sorted, dtype=np.int64) + AFTER_S
    for i, (v, t) in enumerate(zip(values, times)):
        lo, hi = bisect.bisect_left(amt_sorted, v - TOL_SAT), bisect.bisect_right(amt_sorted, v + TOL_SAT)
        if hi > lo:
            n[i] = int(np.count_nonzero((start[lo:hi] <= t) & (t <= end[lo:hi])))
    return n


def selftest():
    amt = [1000, 5000, 5001, 9000]; created = [100, 100, 5000, 100]; valid = [600, 600, 600, 600]
    got = match([1001, 5000, 5000, 5000, 9000, 777], [200, 300, 5200, 6200, 100 + 600 + AFTER_S + 1, 200], amt, created, valid)
    # within 2 sat and live; 5000 early: one offer; 5000 at 5200: the old offer is still inside its window and the new one is live; 5000 at 6200: only the new one; too late; no such amount
    assert got.tolist() == [1, 1, 2, 1, 0, 0], got
    assert match([1001], [200], amt, created, valid, shift=86400).tolist() == [0]
    return True


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--blocks", type=int, default=0, help="newest N blocks only (0 = the whole index)")
    args = ap.parse_args()
    selftest()
    con = duckdb.connect(); con.execute("set threads=2; set memory_limit='700MB'")
    off = con.execute(f"""select "timestamp" created, coin_from, coin_to, cast(round(cast(amount_from_str as double) * 1e8) as bigint) af,
                          cast(round(cast(amount_to_str as double) * 1e8) as bigint) at_, time_valid, addr_from
                          from read_parquet('{LIFE}/**/*.parquet') where coin_from = 'BTC' or coin_to = 'BTC'""").df()
    off["btc_sat"] = np.where(off.coin_from == "BTC", off.af, off.at_)
    off = off[off.btc_sat > 0].sort_values("btc_sat")
    t_off_max = int(off.created.max())
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    tip, = db.execute("select last_height from scan_state where coin='BTC'").fetchone()
    lo_h, = db.execute("select min(height) from p2sh_outputs where coin='BTC'").fetchone()
    if args.blocks:
        lo_h = max(lo_h, tip - args.blocks)
    rows = db.execute("select txid, vout, value, time, height, txinfo from p2sh_outputs where coin='BTC' and height >= ? and otype='p2wsh'", (lo_h,)).fetchall()
    ev = db.execute("select prev_txid, prev_vout, kind, confidence, block_time, lock_time, amount_sat from swap_events where coin='BTC' and block_time >= ? "
                    "and kind in ('adaptor_lock_spend','adaptor_refund_tx')", (int(time.time()) - 12 * 86400,)).fetchall()
    t_lo, = db.execute("select time from blocks where coin='BTC' and height = ?", (lo_h,)).fetchone()
    db.close()
    txid = [r[0] for r in rows]; vout = [r[1] for r in rows]
    val = np.array([r[2] for r in rows], dtype=np.int64); tim = np.array([r[3] for r in rows], dtype=np.int64)
    keep = tim <= t_off_max                                    # offers are exported nightly: later outputs cannot be judged
    amt, cre, vld = off.btc_sat.tolist(), off.created.tolist(), off.time_valid.tolist()
    n_real = match(val, tim, amt, cre, vld)
    n_null = match(val, tim, amt, cre, vld, shift=3 * 86400)
    fate = {(p, v): (k, c, bt, lt) for p, v, k, c, bt, lt, a in ev}
    now = t_off_max
    res = {"window": {"from_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_lo)), "to_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_off_max)), "days": (t_off_max - t_lo) / 86400.0,
                      "blocks": int(tip - lo_h)},
           "p2wsh_outputs": int(keep.sum()), "offers_with_a_btc_leg": int(len(off)),
           "matched_to_a_live_offer": int(((n_real > 0) & keep).sum()), "matched_under_the_null_3_days_shift": int(((n_null > 0) & keep).sum())}
    counts = {"strong swap lock": 0, "refund": 0, "other 2-of-2 spend (likely or candidate tier)": 0, "not seen spent as a 2-of-2": 0}
    old = {"strong swap lock": 0, "refund": 0, "other 2-of-2 spend (likely or candidate tier)": 0, "not seen spent as a 2-of-2": 0}
    lead = []
    for i in np.flatnonzero((n_real > 0) & keep):
        f = fate.get((txid[i], vout[i]))
        key = "not seen spent as a 2-of-2" if f is None else ("strong swap lock" if f[0] == "adaptor_lock_spend" and f[1] == "strong" else ("refund" if f[0] == "adaptor_refund_tx" else "other 2-of-2 spend (likely or candidate tier)"))
        counts[key] += 1
        if tim[i] <= now - SETTLE_S:
            old[key] += 1
        if key == "strong swap lock":
            lead.append((f[2] - tim[i]) / 60.0)
    res["what_the_matched_outputs_became"] = counts
    res["matched_outputs_older_than_6h"] = old
    tot_old = sum(old.values())
    res["precision_strong_or_refund_among_matched_older_than_6h"] = (old["strong swap lock"] + old["refund"]) / tot_old if tot_old else None
    # recall: strong swaps whose lock was mined inside the window
    idx = {(txid[i], vout[i]): i for i in np.flatnonzero(keep)}
    strong = [(p, v, bt, lt) for (p, v), (k, c, bt, lt) in fate.items() if k == "adaptor_lock_spend" and c == "strong" and lt is not None and t_lo <= lt <= t_off_max]
    in_index = [idx[(p, v)] for p, v, bt, lt in strong if (p, v) in idx]
    res["strong_swaps_with_lock_in_window"] = len(strong)
    res["of_which_lock_output_in_the_p2wsh_index"] = len(in_index)
    res["of_which_matched_at_lock_time"] = int(sum(n_real[i] > 0 for i in in_index))
    res["recall_at_lock_time"] = res["of_which_matched_at_lock_time"] / len(in_index) if in_index else None
    res["one_offer_only_share_of_matches"] = float(np.mean(n_real[(n_real > 0) & keep] == 1)) if res["matched_to_a_live_offer"] else None
    res["minutes_gained_lock_to_spend_p10_p50_p90"] = [float(np.quantile(lead, q)) for q in (0.1, 0.5, 0.9)] if lead else None
    # refinements: features of the transaction that created the output (stored with the index) and of the amount itself
    info = [json.loads(r[5]) if r[5] else {} for r in rows]
    lt0v2 = np.array([(d.get("lt") == 0 and d.get("v") == 2) for d in info])
    small_tx = np.array([(d.get("nout", 99) <= 2) for d in info])
    not_round = (val % 1000) != 0
    strong_set = set(in_index)
    is_strong = np.zeros(len(val), bool); is_strong[list(strong_set)] = True
    settled = tim <= now - SETTLE_S
    rules = {"amount equals a live offer": (n_real > 0), "and lock transaction has nLockTime 0, version 2": (n_real > 0) & lt0v2,
             "and at most 2 outputs": (n_real > 0) & lt0v2 & small_tx, "and the amount is not a multiple of 1,000 sat": (n_real > 0) & lt0v2 & small_tx & not_round,
             "and exactly one offer has that amount": (n_real == 1) & lt0v2 & small_tx & not_round}
    res["rules"] = {}
    for name, m in rules.items():
        m = m & keep
        hit = int((m & is_strong).sum())
        res["rules"][name] = {"flagged": int(m.sum()), "flagged_per_day": float(m.sum() / res["window"]["days"]), "of_which_strong_swaps": hit,
                              "precision_older_than_6h": float((m & is_strong & settled).sum() / max(1, (m & settled).sum())), "recall": hit / len(in_index) if in_index else None}
    res["per_day"] = {"p2wsh_outputs": res["p2wsh_outputs"] / res["window"]["days"], "matched": res["matched_to_a_live_offer"] / res["window"]["days"],
                      "matched_null": res["matched_under_the_null_3_days_shift"] / res["window"]["days"]}
    with open(OUT, "w") as fh:
        json.dump(res, fh, indent=1)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
