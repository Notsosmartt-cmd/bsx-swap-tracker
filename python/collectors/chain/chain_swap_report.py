"""Report on the on-chain swap scan (read-only; reads <data_dir>/chainscan.sqlite, Kraken bbo and the offer tables; writes CSVs next to the database).

  python3 python/collectors/chain/chain_swap_report.py            # all sections, prints a summary, writes report_*.csv

Sections (plan: docs/method.md, section 10):
  1. events per coin, day, kind and confidence tier
  2. secret-hash swaps: the two redeems of one swap share a secret hash; legs, USD volume, executed rate vs the Kraken cross, markout at +1/+5/+22 min
  3. attribution: offers live at that time on that pair whose rate matches the executed rate (maker address, auto-accept type)
  4. SMSG cross-check: hourly adaptor-swap events on all scanned chains vs the hourly DLEAG-proof negotiation count
Prices come from Kraken USD books (bbo, 10 s mid); coins without a Kraken USD pair, and times before 2026-10-11, are left unpriced.
"""
import csv
import glob
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict

import duckdb
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "analysis", "chainscan")
DB = os.path.join(OUT, "chainscan.sqlite")
USD = {"BTC": "BTC/USD", "LTC": "LTC/USD", "DOGE": "DOGE/USD", "DASH": "DASH/USD", "BCH": "BCH/USD", "DCR": "DCR/USD"}
HORIZONS = [60, 300, 1320]


def write_csv(name, rows):
    if not rows:
        return
    keys = []
    for r in rows:                                   # rows can have different optional columns: write the union
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, restval="")
        w.writeheader()
        w.writerows(rows)


def load_prices(t0, t1):
    """{coin: (times_s array, mid array)} at 10 s resolution between t0 and t1 (unix s)"""
    con = duckdb.connect()
    con.execute("SET memory_limit='1500MB'; SET threads=2")
    days = sorted(os.path.basename(p)[5:] for p in glob.glob(os.path.join(DATA, "parquet", "bbo", "date=*")))
    last_pq = days[-1] if days else "00000000"
    parts = []
    if days:
        parts.append("select exch_ts_ms, symbol, (bid+ask)/2 mid from read_parquet('%s/parquet/bbo/*/*.parquet', hive_partitioning=true) where exch_ts_ms between %d and %d" % (DATA, t0 * 1000, t1 * 1000))
    for p in sorted(glob.glob(os.path.join(DATA, "kraken", "cross", "bbo_*.csv"))):
        if os.path.basename(p)[4:12] > last_pq:
            parts.append("select exch_ts_ms, symbol, (bid+ask)/2 mid from read_csv('%s', strict_mode=false) where exch_ts_ms between %d and %d and bid>0 and ask>0" % (p, t0 * 1000, t1 * 1000))
    if not parts:
        return {}
    syms = ",".join("'%s'" % s for s in USD.values())
    q = "select symbol, cast(exch_ts_ms/10000 as bigint)*10 t, avg(mid) m from (%s) where symbol in (%s) group by 1,2 order by 1,2" % (" union all ".join(parts), syms)
    df = con.execute(q).df()
    inv = {v: k for k, v in USD.items()}
    out = {}
    for sym, g in df.groupby("symbol"):
        out[inv[sym]] = (g["t"].to_numpy(dtype=np.int64), g["m"].to_numpy(dtype=np.float64))
    return out


def px(prices, coin, t, max_gap=120):
    if coin not in prices:
        return None
    ts, m = prices[coin]
    i = np.searchsorted(ts, t, side="right") - 1
    if i < 0 or t - ts[i] > max_gap:
        return None
    return float(m[i])


def spearman(a, b):
    from scipy import stats
    if len(a) < 5 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    return float(stats.spearmanr(a, b)[0])


def main():
    if not os.path.exists(DB):
        sys.exit("no scan database yet: run python/collectors/chain_swap_scan.py first")
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    db.row_factory = sqlite3.Row
    states = {r["coin"]: dict(r) for r in db.execute("select * from scan_state")}
    print("scan state:", {c: (s["last_height"], s["state"]) for c, s in states.items()})

    # 1. events per day
    rows = db.execute("""select coin, strftime('%Y-%m-%d', block_time, 'unixepoch') day, kind, confidence, count(*) n, sum(amount_sat) sat
                         from swap_events group by 1,2,3,4 order by 2,1""").fetchall()
    write_csv("report_events_by_day.csv", [dict(r) for r in rows])
    tot = defaultdict(int)
    for r in rows:
        tot[(r["coin"], r["kind"], r["confidence"])] += r["n"]
    print("events by coin/kind/tier:")
    for k, v in sorted(tot.items()):
        print("  %-5s %-22s %-10s %d" % (k[0], k[1], k[2], v))

    # 2. secret-hash swaps
    ev = db.execute("select * from swap_events where kind in ('htlc_redeem','htlc_refund','htlc_spend_unknown') order by block_time").fetchall()
    by_hash = defaultdict(list)
    for e in ev:
        by_hash[e["secret_hash"]].append(e)
    t_all = [e["block_time"] for e in ev]
    prices = load_prices(min(t_all) - 3600, max(t_all) + 1800 * 2) if t_all else {}
    swaps, unmatched = [], []
    for sh, es in by_hash.items():
        red = [e for e in es if e["kind"] == "htlc_redeem"]
        legs = {}
        for e in red:
            legs.setdefault(e["coin"], e)
        if len(legs) >= 2:
            (ca, ea), (cb, eb) = sorted(legs.items(), key=lambda kv: kv[1]["block_time"])[:2]
            rec = {"secret_hash": sh[:16], "coin_a": ca, "coin_b": cb, "amount_a": (ea["amount_sat"] or 0) / 1e8 if ea["amount_sat"] else None,
                   "amount_b": (eb["amount_sat"] or 0) / 1e8 if eb["amount_sat"] else None, "t_a": ea["block_time"], "t_b": eb["block_time"]}
            t = max(ea["block_time"], eb["block_time"])
            pa, pb = px(prices, ca, t), px(prices, cb, t)
            if rec["amount_a"] and rec["amount_b"]:
                rec["rate_b_per_a"] = rec["amount_b"] / rec["amount_a"]
                if pa and pb:
                    k = pa / pb
                    rec["kraken_b_per_a"] = k
                    rec["premium_bps"] = (rec["rate_b_per_a"] / k - 1) * 1e4
                    rec["usd"] = rec["amount_a"] * pa
                    for G in HORIZONS:
                        pa2, pb2 = px(prices, ca, t + G), px(prices, cb, t + G)
                        if pa2 and pb2:
                            rec["move_bps_%ds" % G] = ((pa2 / pb2) / k - 1) * 1e4        # change of the Kraken cross after the swap (A priced in B)
            swaps.append(rec)
        else:
            unmatched.append({"secret_hash": sh[:16], "coins": ",".join(sorted({e["coin"] for e in es})), "kinds": ",".join(sorted({e["kind"] for e in es})),
                              "n": len(es), "t": min(e["block_time"] for e in es)})
    write_csv("report_secret_hash_swaps.csv", swaps)
    write_csv("report_secret_hash_one_legged.csv", unmatched)
    refunds = sum(1 for es in by_hash.values() if any(e["kind"] == "htlc_refund" for e in es))
    print("secret-hash contracts seen: %d (two redeemed legs linked: %d, one leg only or refunded: %d; with a refund: %d)" % (len(by_hash), len(swaps), len(unmatched), refunds))
    if swaps:
        prem = [s["premium_bps"] for s in swaps if "premium_bps" in s]
        if prem:
            print("  executed rate vs Kraken cross at redeem time (bps, signed): median %.0f, p10 %.0f, p90 %.0f (n=%d)" % (np.median(prem), np.percentile(prem, 10), np.percentile(prem, 90), len(prem)))
        usd = [s["usd"] for s in swaps if "usd" in s]
        if usd:
            print("  priced swaps: %d, USD median %.0f, total %.0f" % (len(usd), np.median(usd), sum(usd)))

    # 3. attribution to offers (secret-hash swaps with both amounts)
    try:
        con = duckdb.connect()
        con.execute("SET memory_limit='1500MB'; SET threads=2")
        offers = con.execute("""select coin_from, coin_to, amount_from_str af, amount_to_str ato, min_bid_amount_str mb, rate, epoch(first_seen) fs, epoch(last_seen) ls, addr_from, auto_accept_type aa
                                from read_parquet('%s/parquet/offer_lifecycle_site/*.parquet')""" % DATA).df()
    except Exception as e:  # noqa: BLE001
        offers = None
        print("offers not loaded:", type(e).__name__)
    match_rows = []
    if offers is not None:
        listed = {frozenset((x, y)) for x, y in zip(offers.coin_from, offers.coin_to)}
        for s_ in swaps:
            s_["bsx_pair_listed"] = frozenset((s_["coin_a"], s_["coin_b"])) in listed       # False: the same standard contract is also used by other software (e.g. Decred's DEX for DCR/BTC)
        write_csv("report_secret_hash_swaps.csv", swaps)
        nl = sum(1 for s_ in swaps if s_["bsx_pair_listed"])
        print("linked secret-hash swaps on pairs BasicSwap lists: %d of %d (the rest use the same standard contract but another venue)" % (nl, len(swaps)))
        for s in swaps:
            if not s.get("rate_b_per_a"):
                continue
            t, tol = max(s["t_a"], s["t_b"]), 0.003
            ca, cb, r = s["coin_a"], s["coin_b"], s["rate_b_per_a"]
            live = offers[(offers.fs <= t) & (offers.ls + 600 >= t)]
            m1 = live[(live.coin_from == ca) & (live.coin_to == cb) & ((live.ato / live.af / r - 1).abs() < tol)]
            m2 = live[(live.coin_from == cb) & (live.coin_to == ca) & ((live.af / live.ato / r - 1).abs() < tol)]
            cand = m1.assign(side="maker_gives_" + ca)._append(m2.assign(side="maker_gives_" + cb)) if len(m1) + len(m2) else m1
            makers = sorted(set(cand.addr_from)) if len(cand) else []
            match_rows.append({"secret_hash": s["secret_hash"], "pair": "%s/%s" % (ca, cb), "rate_b_per_a": round(r, 8), "candidate_offers": len(cand), "candidate_makers": len(makers),
                               "maker_if_unique": makers[0] if len(makers) == 1 else "", "auto_accept_types": ",".join(str(int(x)) for x in sorted(set(cand.aa.dropna()))) if len(cand) else ""})
        write_csv("report_attribution.csv", match_rows)
        if match_rows:
            n1 = sum(1 for m in match_rows if m["candidate_makers"] == 1)
            n0 = sum(1 for m in match_rows if m["candidate_makers"] == 0)
            print("attribution: %d linked swaps; %d with a single candidate maker, %d with several, %d with none" % (len(match_rows), n1, len(match_rows) - n1 - n0, n0))

    # 4. SMSG cross-check
    try:
        smsg = json.load(open(os.path.join(DATA, "analysis", "smsg_swap_activity.json")))["hourly"]
        ad = db.execute("""select (block_time/3600)*3600 hr, count(*) n from swap_events
                           where protocol in ('xmr_adaptor','xmr_bch_covenant') and confidence in ('certain','strong') group by 1""").fetchall()
        adm = {r["hr"]: r["n"] for r in ad}
        hrs = [h for h in smsg if h["messages"] > 300]
        lo, hi = (min(t_all) if t_all else 0), 0
        a, b = [], []
        for h in hrs:
            a.append(h["dleag_proofs"] / 2.0)
            b.append(adm.get(h["bucket_ts"], 0))
        rho = spearman(a, b)
        write_csv("report_smsg_crosscheck.csv", [{"hour_utc": h["hour_utc"], "smsg_negotiations": h["dleag_proofs"] / 2.0, "scan_adaptor_events": adm.get(h["bucket_ts"], 0)} for h in hrs])
        print("SMSG cross-check over %d complete hours: SMSG negotiations %.1f, scanned certain+strong adaptor events %d, Spearman %s" % (len(hrs), sum(a), sum(b), "n/a" if rho is None else "%.2f" % rho))
    except Exception as e:  # noqa: BLE001
        print("SMSG cross-check skipped:", type(e).__name__, str(e)[:80])


if __name__ == "__main__":
    main()
