#!/usr/bin/env python3
# Usage: python3 python/collectors/trap_labels.py <repository root>   (read-only; light: duckdb 1.5 GB cap, 2 threads; about 1 min)
# Meant to run once a day (here: the nightly job). Outputs data/analysis/trap_labels_latest.csv (+ dated copy and summary json).
"""Label offers by on-chain outcome and compare their terms (read-only; writes only data/analysis/).
Failed = an XMR-adaptor A-lock that was refunded (refund tx, certain by script). Completed = an A-lock spent the success way (tier strong).
Each event is matched to the public-site offer whose full amount on that coin equals the lock value (+-2 sat) and that was live at the lock
(same rule as python/collectors/alock_offer_match.py). Reverse = the matched amount is the offer's amount_to, i.e. the taker locked first.
Features per matched offer: price vs Kraken at creation and at the lock (taker's view, + = better than Kraken), size in USD, validity, lock time,
auto-accept, offer age at the lock, protocol, and how many offers with identical terms (except price, size, address) appeared within 24 h before."""
import csv, json, os, sys
import duckdb

ROOT = sys.argv[1]
os.chdir(ROOT)
c = duckdb.connect()
c.execute("set memory_limit='1500MB'; set threads=2")
S = "read_parquet('data/parquet/site_snapshots/*/*.parquet', hive_partitioning=true)"
T = "read_parquet('data/parquet/ticker/*/*.parquet', hive_partitioning=true)"

import sqlite3
T0 = 1791604800    # 2026-10-10 00:00 UTC: first day of public-site offer data
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector_settings import S as SETTINGS  # noqa: E402  data folder of the scan database (settings file or the default layout)
db = sqlite3.connect("file:%s?mode=ro" % os.path.join(SETTINGS.data_dir, "chainscan.sqlite"), uri=True, timeout=30)
ev = db.execute("""select coin, case when kind='adaptor_refund_tx' then 'failed' else 'completed' end, amount_sat, lock_time, block_time, txid from swap_events
                   where coin in ('BTC','LTC','PART') and block_time >= ? and amount_sat is not null and lock_time is not null
                   and ((kind='adaptor_refund_tx' and confidence='certain') or (kind='adaptor_lock_spend' and confidence='strong'))""", (T0,)).fetchall()
db.close()
c.execute("create temp table ev(coin varchar, outcome varchar, lock_sat bigint, lock_time bigint, spend_time bigint, txid varchar)")
c.executemany("insert into ev values (?,?,?,?,?,?)", ev)

c.execute(f"""create temp table offers as select msg_id, min(epoch(snapshot_utc))::bigint first_seen, max(epoch(snapshot_utc))::bigint last_seen,
  any_value(timestamp) created, any_value(coin_from) cf, any_value(coin_to) ct, any_value(amount_from) a_from, any_value(amount_to) a_to,
  any_value(amount_from_str) amt_from, any_value(amount_to_str) amt_to, any_value(rate) rate, any_value(time_valid) ttl, any_value(lock_type) lock_type, any_value(lock_value) lock_value,
  any_value(auto_accept_type) aa, any_value(protocol_version) pv, any_value(min_bid_amount_str) mn, any_value(fee_rate_from) frf, any_value(fee_rate_to) frt, any_value(addr_from) addr
  from {S} group by msg_id""")
from offer_match_quality import PRICE_PAIRS, REVERSE_COINS, usd_value  # noqa: E402
_case = " ".join("when '%s' then '%s'" % kv for kv in PRICE_PAIRS.items())
c.execute(f"""create temp table px as select ts, case pair {_case} end coin, (bid+ask)/2 mid
  from {T} where pair in ({",".join("'%s'" % p for p in PRICE_PAIRS)})""")
# the lock side follows BasicSwap's is_reverse_ads_bid (review item 10, 2026-11-20): reverse = coin_from scriptless or without segwit, lock on coin_to
_rev = ",".join("'%s'" % x for x in sorted(REVERSE_COINS))
c.execute("create temp table m as select e.*, o.*, case when o.cf in (%s) then 'reverse' else 'normal' end direction "
          "from ev e join offers o on ((o.cf in (%s) and o.ct=e.coin and o.ct not in (%s) and abs(o.a_to-e.lock_sat)<=2) "
          "or (o.cf not in (%s) and o.cf=e.coin and abs(o.a_from-e.lock_sat)<=2)) "
          "and o.first_seen<=e.lock_time+600 and o.last_seen>=e.lock_time-3600" % (_rev, _rev, _rev, _rev))


import bisect
PX = {}
for coin, ts, mid in c.execute("select coin, ts, mid from px where mid is not null order by coin, ts").fetchall():
    PX.setdefault(coin, ([], []))
    PX[coin][0].append(ts); PX[coin][1].append(mid)
def usd(coin, t):
    if coin not in PX or t is None: return None
    a = PX[coin]; i = bisect.bisect_right(a[0], t) - 1
    return a[1][i] if i >= 0 and t - a[0][i] < 900 else None
rows = c.execute("""select outcome, direction, coin, lock_sat, lock_time, spend_time, txid, msg_id, cf, ct, amt_from, amt_to, rate, ttl, lock_type, lock_value, aa, pv, created, first_seen, last_seen, addr, mn, frf, frt
  from m order by lock_time""").fetchall()
cols = [d[0] for d in c.description]
out = []
for r in rows:
    d = dict(zip(cols, r))
    def prem(fu, tu):   # taker receives coin_from and pays coin_to; Kraken fair rate = USD(from)/USD(to); + = taker pays less than Kraken
        r = d["amt_to"] / d["amt_from"] if d["amt_from"] else None      # decimal rate: coin_to per coin_from (the 'rate' column is in atomic units)
        return None if not (fu and tu and r) else round(((fu / tu) / r - 1) * 100, 2)
    d["prem_create_pct"] = prem(usd(d["cf"], d["created"]), usd(d["ct"], d["created"]))
    d["prem_lock_pct"] = prem(usd(d["cf"], d["lock_time"]), usd(d["ct"], d["lock_time"]))
    d["usd"] = usd_value(d["coin"], d["lock_sat"], lambda coin: usd(coin, d["lock_time"]), d["amt_from"], d["cf"], d["amt_to"], d["ct"])
    d["age_at_lock_h"] = round((d["lock_time"] - d["created"]) / 3600, 2) if d["created"] else None
    d["lock_to_spend_h"] = round((d["spend_time"] - d["lock_time"]) / 3600, 2) if d["spend_time"] else None
    # siblings: offers with the same maker-chosen terms (pair, validity, lock, auto-accept, protocol, fee rates) created within 24 h before this one
    d["same_terms_24h"] = c.execute("""select count(*) from offers where cf=? and ct=? and ttl=? and lock_value=? and aa=? and pv=? and frf=? and frt=? and created between ?-86400 and ?""",
                                    [d["cf"], d["ct"], d["ttl"], d["lock_value"], d["aa"], d["pv"], d["frf"], d["frt"], d["created"], d["created"]]).fetchone()[0]
    d["addr"] = (d["addr"] or "")[:6] + "..."      # short form only
    out.append(d)

from offer_match_quality import annotate, summary as quality_summary  # noqa: E402  candidate count, round amount, match quality; deterministic order (2026-11-20)
out = annotate(out)

os.makedirs("data/analysis", exist_ok=True)
import datetime as _dt
STAMP = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
for name in ("trap_labels_%s.csv" % STAMP, "trap_labels_latest.csv"):   # dated copy + the one other tools read
  with open(os.path.join("data/analysis", name), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)

nev = c.execute("select outcome, count(*) from ev group by 1").fetchall()
print("events in window (all, before matching):", nev)
print("matched offer rows:", len(out), " (an event can match more than one offer: same round amount)")
print("matched events by match quality:", quality_summary(out))
uniq = {}
for d in out:
    uniq.setdefault((d["txid"] if "txid" in d else d["lock_time"], d["lock_sat"]), []).append(d)
print()
print("FAILED (refunded) matches:")
for d in out:
    if d["outcome"] == "failed":
        print(f"  {d['cf']}>{d['ct']} {d['direction']:7} lock {d['lock_sat']} usd {d['usd']} prem@create {d['prem_create_pct']} prem@lock {d['prem_lock_pct']} ttl {d['ttl']//3600 if d['ttl'] else None}h lock {round(d['lock_value']/3600,1) if d['lock_value'] else None}h aa {d['aa']} age@lock {d['age_at_lock_h']}h refund after {d['lock_to_spend_h']}h siblings24h {d['same_terms_24h']} pv {d['pv']}")


def summ(sel):
    xs = [d for d in out if sel(d)]
    if not xs:
        return "none"
    import statistics as st
    def med(k):
        v = [d[k] for d in xs if d[k] is not None]
        return round(st.median(v), 2) if v else None
    share = lambda f: round(100 * sum(1 for d in xs if f(d)) / len(xs))
    return {"n": len(xs), "median_prem_create": med("prem_create_pct"), "median_prem_lock": med("prem_lock_pct"), "median_usd": med("usd"), "median_ttl_h": round((med("ttl") or 0) / 3600, 1),
            "median_lock_h": round((med("lock_value") or 0) / 3600, 1), "median_age_at_lock_h": med("age_at_lock_h"), "pct_ttl_ge_24h": share(lambda d: (d["ttl"] or 0) >= 86400),
            "pct_age_ge_3h": share(lambda d: (d["age_at_lock_h"] or 0) >= 3), "pct_prem_create_ge_1": share(lambda d: (d["prem_create_pct"] or -99) >= 1),
            "pct_prem_lock_ge_1": share(lambda d: (d["prem_lock_pct"] or -99) >= 1), "pct_usd_le_25": share(lambda d: (d["usd"] or 1e9) <= 25), "median_siblings_24h": med("same_terms_24h")}


S_ = {}
for lab, sel in [("failed_reverse", lambda d: d["outcome"] == "failed" and d["direction"] == "reverse"),
                 ("completed_reverse", lambda d: d["outcome"] == "completed" and d["direction"] == "reverse"),
                 ("failed_normal", lambda d: d["outcome"] == "failed" and d["direction"] == "normal"),
                 ("completed_normal", lambda d: d["outcome"] == "completed" and d["direction"] == "normal")]:
    S_[lab] = summ(sel)
    print(lab, S_[lab])
json.dump({"summary": S_, "events": nev, "match_quality": quality_summary(out), "note": "see trap_labels_20261116.csv (read-only analysis)"}, open("data/analysis/trap_labels_%s.json" % STAMP, "w"), indent=1, default=str)
