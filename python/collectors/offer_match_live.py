"""Offer match after every chain scan: the nightly match of python/collectors/trap_labels.py, run incrementally (docs/method.md).

The chain swap scan sees a completed or refunded swap lock (coin, amount, time).  The match finds the public offer whose full amount on that coin equals
the lock (+-2 sat) and that was listed at the lock time; the offer gives maker, direction, price and terms.  The nightly job does this from the Parquet
tables, which hold closed days only, so a swap from today could not be matched before tomorrow night.  This module matches every event since the
nightly file's coverage ended, using the same rule and the same columns, from:
  offers   Parquet site_snapshots for a few closed days of context + the raw board snapshots (<snapshots_dir>/*.json.gz) of the days
           after, parsed once and cached per day (each run reads only the snapshot files that are new since the last run)
  prices   Parquet ticker for the closed days + the raw Kraken ticker CSV of the days after
  events   strong lock spends and certain refunds on BTC and LTC from the scan database (read-only)
Writes (atomically, temp file + rename):
  data/analysis/offer_match_live.csv          the rows this module matched (events with spend or refund time >= live_start)
  data/analysis/trap_labels_latest.csv        the newest nightly file's rows before live_start + the live rows (the file dashboards read)
  data/analysis/offer_match_live_status.json  what the run did
  data/analysis/offer_match_early.csv         (2026-11-19) every lock flagged by the live chain watch in the last 48 h (<data_dir>/early_locks.csv),
                                              offer-matched with the same rule, with status pending / completed / failed / spent:<kind> (read by dashboards)
  data/analysis/offer_match_live/             per-day cache of parsed raw snapshots, and the run lock
Read-only towards every input.  Started by bsx-offer-match.service after each BTC and LTC scan (OnSuccess= in the scan units).
Run by hand:   python3 python/collectors/offer_match_live.py [--as-of-day YYYYMMDD --out-dir DIR --nightly-csv FILE] [--selftest]
"""
import argparse, bisect, csv, datetime as dt, fcntl, glob, gzip, json, os, re, sqlite3, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # run with python -I: the module folder is not on the path by itself
from collector_settings import S as SETTINGS  # noqa: E402
from offer_match_quality import PRICE_PAIRS, annotate, lock_direction, usd_value  # noqa: E402  match quality, lock side, USD (2026-11-20)
DATA = os.path.join(ROOT, "data")
SNAP_DIR = SETTINGS.snapshots_dir
TICK_DIR = os.path.join(DATA, "kraken", "ticker")
PQ = os.path.join(DATA, "parquet")
DB = os.path.join(SETTINGS.data_dir, "chainscan.sqlite")
ANALYSIS = os.path.join(DATA, "analysis")
LOG = os.path.join(DATA, "logs", "offer_match_live.log")

T0 = 1791604800                 # 2026-10-10 00:00 UTC, first day of public-board data (as trap_labels.py)
TOL_SAT, BEFORE_S, AFTER_S = 2, 600, 3600   # trap_labels rule: amount within 2 sat; first_seen <= lock + 600; last_seen >= lock - 3600
MARGIN_S = 3600                 # live rows start one hour before the nightly coverage ends
CTX_DAYS = 4                    # closed days of Parquet offers and prices read for context (older locks, terms counted 24 h back, price at creation)
MAX_LIVE_DAYS = 7               # refuse to rebuild more raw days than this (nightly job broken for a week: fix that first)
READY_S = 15                    # a snapshot file younger than this may still be being written: leave it for the next run
PAIRS = PRICE_PAIRS                               # offer_match_quality.py (DOGE and DASH added 2026-11-20)
INT = {"timestamp", "amount_from", "amount_to", "lock_type", "lock_value", "time_valid", "auto_accept_type", "protocol_version", "fee_rate_from", "fee_rate_to"}
FLOAT = {"amount_from_str", "amount_to_str", "min_bid_amount_str", "rate"}
# offer record: raw field -> column name used by trap_labels.py
OFFER_COLS = (("timestamp", "created"), ("coin_from", "cf"), ("coin_to", "ct"), ("amount_from", "a_from"), ("amount_to", "a_to"), ("amount_from_str", "amt_from"),
              ("amount_to_str", "amt_to"), ("rate", "rate"), ("time_valid", "ttl"), ("lock_type", "lock_type"), ("lock_value", "lock_value"),
              ("auto_accept_type", "aa"), ("protocol_version", "pv"), ("min_bid_amount_str", "mn"), ("fee_rate_from", "frf"), ("fee_rate_to", "frt"), ("addr_from", "addr"))
FIELDS = ["outcome", "direction", "coin", "lock_sat", "lock_time", "spend_time", "txid", "msg_id", "cf", "ct", "amt_from", "amt_to", "rate", "ttl", "lock_type",
          "lock_value", "aa", "pv", "created", "first_seen", "last_seen", "addr", "mn", "frf", "frt", "prem_create_pct", "prem_lock_pct", "usd", "age_at_lock_h",
          "lock_to_spend_h", "same_terms_24h", "n_candidates", "round_amount", "match_quality"]   # last three: offer_match_quality.py (2026-11-20)
TERMS = ("cf", "ct", "ttl", "lock_value", "aa", "pv", "frf", "frt")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " " + msg
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def day_start(day):
    return int(dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp())


def day_of(ts):
    return time.strftime("%Y%m%d", time.gmtime(ts))


def days_between(a, b):
    """days a..b inclusive, YYYYMMDD strings"""
    out, t = [], day_start(a)
    while day_of(t) <= b:
        out.append(day_of(t)); t += 86400
    return out


def cv(k, v):
    """the same field conversion as the board's Parquet files, for the fields used here"""
    if v is None:
        return None
    try:
        if k in INT:
            return int(float(v)) if not isinstance(v, str) or "." in v else int(v)
        if k in FLOAT:
            return float(v)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, (str, type(None))) else str(v)


def atomic_write(path, write_fn, mode_from=None):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_" + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            write_fn(fh)
        if mode_from and os.path.exists(mode_from):
            os.chmod(tmp, os.stat(mode_from).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ offers
def merge_offer(idx, mid, first, last, rec):
    o = idx.get(mid)
    if o is None:
        idx[mid] = dict(rec, first_seen=first, last_seen=last)
    else:
        o["first_seen"] = min(o["first_seen"], first)
        o["last_seen"] = max(o["last_seen"], last)


def raw_day(day, cache_dir, now):
    """parsed offers of one raw day, extended by the snapshot files that are new since the cached run; returns (offers, new_files, bad_files)"""
    cpath = os.path.join(cache_dir, f"raw_offers_{day}.json")
    cache = {"day": day, "last_file": "", "offers": {}, "bad": 0}
    if os.path.exists(cpath):
        try:
            cache = json.load(open(cpath))
        except (OSError, ValueError):
            pass
    new = bad = 0
    for f in sorted(glob.glob(os.path.join(SNAP_DIR, f"{day}T*.json.gz"))):
        name = os.path.basename(f)
        if name <= cache["last_file"]:
            continue
        if now - os.path.getmtime(f) < READY_S:
            break                                           # still being written; this and later files wait for the next run
        try:
            with gzip.open(f, "rt", encoding="utf8") as fh:
                offers = json.load(fh)["offers"]
            if not isinstance(offers, list):
                raise ValueError
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            bad += 1; cache["last_file"] = name; continue
        snap = int(dt.datetime.strptime(re.search(r"(\d{8}T\d{6}Z)", name).group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())
        for o in offers:
            mid = o.get("msg_id")
            if mid is None:
                continue
            c = cache["offers"].get(mid)
            if c is None:
                cache["offers"][mid] = [snap, snap] + [cv(k, o.get(k)) for k, _ in OFFER_COLS]
            else:
                c[0] = min(c[0], snap); c[1] = max(c[1], snap)
        cache["last_file"] = name; new += 1
    cache["bad"] = cache.get("bad", 0) + bad
    if new or bad or not os.path.exists(cpath):
        atomic_write(cpath, lambda fh: json.dump(cache, fh, separators=(",", ":")))
    return cache["offers"], new, bad


def parquet_offers(con, days):
    files = [f for d in days for f in glob.glob(os.path.join(PQ, "site_snapshots", f"date={d}", "*.parquet"))]
    if not files:
        return []
    sel = ", ".join(f"any_value({k}) {c}" for k, c in OFFER_COLS)
    return con.execute(f"""select msg_id, min(epoch(snapshot_utc))::bigint, max(epoch(snapshot_utc))::bigint, {sel}
                           from read_parquet(?) group by msg_id""", [files]).fetchall()


def build_offers(con, ctx_days, raw_days, cache_dir, now):
    idx, stats = {}, {"parquet_days": ctx_days, "raw_days": raw_days, "raw_files_new": 0, "raw_files_bad": 0}
    for r in parquet_offers(con, ctx_days):
        merge_offer(idx, r[0], r[1], r[2], dict(zip([c for _, c in OFFER_COLS], r[3:])))
    for d in raw_days:
        offers, new, bad = raw_day(d, cache_dir, now)
        stats["raw_files_new"] += new; stats["raw_files_bad"] += bad
        for mid, v in offers.items():
            merge_offer(idx, mid, v[0], v[1], dict(zip([c for _, c in OFFER_COLS], v[2:])))
    stats["offers"] = len(idx)
    return idx, stats


# ------------------------------------------------------------------ prices
def load_prices(con, pq_days, raw_days):
    rows = []
    files = [f for d in pq_days for f in glob.glob(os.path.join(PQ, "ticker", f"date={d}", "*.parquet"))]
    if files:
        rows += con.execute("select ts, pair, (bid + ask) / 2 from read_parquet(?) where pair in ('XBTUSD','XMRUSD','LTCUSD','BCHUSD')", [files]).fetchall()
    for d in raw_days:
        f = os.path.join(TICK_DIR, f"ticker_{d}.csv")
        if os.path.exists(f):
            rows += con.execute("select ts, pair, (bid + ask) / 2 from read_csv(?, header=true, types={'ts':'BIGINT','pair':'VARCHAR','bid':'DOUBLE','ask':'DOUBLE'}, "
                                "ignore_errors=true) where pair in ('XBTUSD','XMRUSD','LTCUSD','BCHUSD')", [f]).fetchall()
    px = {}
    for ts, pair, mid in sorted((r for r in rows if r[2] is not None and r[0] is not None), key=lambda r: (PAIRS[r[1]], r[0])):
        a = px.setdefault(PAIRS[pair], ([], []))
        a[0].append(ts); a[1].append(mid)
    return px


def usd(px, coin, t):
    if coin not in px or t is None:
        return None
    a = px[coin]; i = bisect.bisect_right(a[0], t) - 1
    return a[1][i] if i >= 0 and t - a[0][i] < 900 else None


# ------------------------------------------------------------------ match
def match(events, idx, px):
    """events: (coin, outcome, lock_sat, lock_time, spend_time, txid).  Same rule, direction and features as trap_labels.py."""
    by = {}                                                  # (coin, side) -> sorted [(amount, msg_id)]; side 'to' = offer's amount_to is in that coin
    terms = {}
    for mid, o in idx.items():
        if o["ct"] is not None and o["a_to"] is not None:
            by.setdefault((o["ct"], "to"), []).append((o["a_to"], mid))
        if o["cf"] is not None and o["a_from"] is not None:
            by.setdefault((o["cf"], "from"), []).append((o["a_from"], mid))
        if all(o[k] is not None for k in TERMS) and o["created"] is not None:
            terms.setdefault(tuple(o[k] for k in TERMS), []).append(o["created"])
    for v in by.values():
        v.sort()
    for v in terms.values():
        v.sort()
    out = []
    for coin, outcome, lock_sat, lock_time, spend_time, txid in events:
        hits = {}
        for side in ("to", "from"):
            arr = by.get((coin, side), [])
            i = bisect.bisect_left(arr, (lock_sat - TOL_SAT, ""))
            while i < len(arr) and arr[i][0] <= lock_sat + TOL_SAT:
                mid = arr[i][1]; o = idx[mid]
                if o["first_seen"] <= lock_time + BEFORE_S and o["last_seen"] >= lock_time - AFTER_S:
                    direction = lock_direction(coin, o["cf"], o["ct"])    # only the side that can hold the lock (is_reverse_ads_bid)
                    if direction == ("reverse" if side == "to" else "normal"):
                        hits[mid] = direction
                i += 1
        for mid, direction in hits.items():
            o = idx[mid]
            d = {"outcome": outcome, "direction": direction, "coin": coin, "lock_sat": lock_sat, "lock_time": lock_time, "spend_time": spend_time, "txid": txid,
                 "msg_id": mid}
            d.update({k: o[k] for k in ("cf", "ct", "amt_from", "amt_to", "rate", "ttl", "lock_type", "lock_value", "aa", "pv", "created", "first_seen", "last_seen",
                                        "addr", "mn", "frf", "frt")})
            r = d["amt_to"] / d["amt_from"] if d["amt_from"] else None

            def prem(fu, tu):
                return None if not (fu and tu and r) else round(((fu / tu) / r - 1) * 100, 2)
            d["prem_create_pct"] = prem(usd(px, d["cf"], d["created"]), usd(px, d["ct"], d["created"]))
            d["prem_lock_pct"] = prem(usd(px, d["cf"], lock_time), usd(px, d["ct"], lock_time))
            d["usd"] = usd_value(coin, lock_sat, lambda c: usd(px, c, lock_time), d["amt_from"], d["cf"], d["amt_to"], d["ct"])
            d["age_at_lock_h"] = round((lock_time - d["created"]) / 3600, 2) if d["created"] else None
            d["lock_to_spend_h"] = round((spend_time - lock_time) / 3600, 2) if spend_time else None
            if all(o[k] is not None for k in TERMS) and o["created"] is not None:
                c = terms[tuple(o[k] for k in TERMS)]
                d["same_terms_24h"] = bisect.bisect_right(c, o["created"]) - bisect.bisect_left(c, o["created"] - 86400)
            else:
                d["same_terms_24h"] = 0
            d["addr"] = (d["addr"] or "")[:6] + "..."
            out.append(d)
    return annotate(out)


def load_events(since):
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    try:
        return db.execute("""select coin, case when kind='adaptor_refund_tx' then 'failed' else 'completed' end, amount_sat, lock_time, block_time, txid from swap_events
                             where coin in ('BTC','LTC','PART') and block_time >= ? and amount_sat is not null and lock_time is not null
                             and ((kind='adaptor_refund_tx' and confidence='certain') or (kind='adaptor_lock_spend' and confidence='strong'))""", (since,)).fetchall()
    finally:
        db.close()


# ------------------------------------------------------------------ early lock flags from the live chain watch (2026-11-19)
EARLY_CSV = os.path.join(SETTINGS.data_dir, "early_locks.csv")
EARLY_WINDOW_S = 48 * 3600
EARLY_FIELDS = FIELDS + ["lock_vout", "first_stage", "height", "status"]


def load_early(path, since):
    """Flagged locks (all four probe rules; python/collectors/chain_watch.py), first sighting per (coin, txid, vout) at or after `since`."""
    out = {}
    try:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("flag") != "1" or r.get("coin") not in ("BTC", "LTC", "PART"):   # PART as in the C++ matcher (2026-11-20)
                    continue
                try:
                    t = int(dt.datetime.fromisoformat(r["seen_utc"]).timestamp())
                    k = (r["coin"], r["txid"], int(r["vout"]))
                    v = int(r["value_sat"])
                except (KeyError, ValueError, TypeError):
                    continue
                if t < since:
                    continue
                if k not in out:
                    out[k] = {"t": t, "value": v, "stage": r.get("stage", ""), "height": r.get("height") or ""}
                elif r.get("height"):
                    out[k]["height"] = r["height"]
    except OSError:
        pass
    return out


def early_status(flags, since, db_path=DB):
    """(coin, txid, vout) -> (status, spend_time): completed (strong lock spend), failed (refund), spent:<kind>/<tier> (spent another way,
    i.e. probably not a swap), or pending (no spend seen yet)."""
    st = {k: ("pending", None) for k in flags}
    if not flags or not os.path.exists(db_path):
        return st
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    try:
        for coin, kind, conf, bt, ptx, pv in db.execute("""select coin, kind, confidence, block_time, prev_txid, prev_vout from swap_events
                                                           where coin in ('BTC','LTC','PART') and block_time >= ? and prev_txid is not null""", (since,)):
            k = (coin, ptx, pv)
            if k in st:
                s = "completed" if (kind == "adaptor_lock_spend" and conf == "strong") else ("failed" if kind == "adaptor_refund_tx" else f"spent:{kind}/{conf}")
                st[k] = (s, bt)
    finally:
        db.close()
    return st


def early_rows(flags, status, idx, px):
    """Offer-match every flag (same rule as the completed swaps); a flag with no offer in the window still gets a row (offer fields empty)."""
    ev = [(k[0], status[k][0], f["value"], f["t"], status[k][1], f"{k[1]}:{k[2]}") for k, f in flags.items()]
    rows, hit = match(ev, idx, px), set()
    for r in rows:
        tx, vout = r["txid"].rsplit(":", 1)
        k = (r["coin"], tx, int(vout))
        hit.add(k)
        r.update(txid=tx, lock_vout=int(vout), first_stage=flags[k]["stage"], height=flags[k]["height"], status=status[k][0])
    for k, f in flags.items():
        if k not in hit:
            rows.append({"outcome": status[k][0], "coin": k[0], "lock_sat": f["value"], "lock_time": f["t"], "spend_time": status[k][1], "txid": k[1],
                         "lock_vout": k[2], "first_stage": f["stage"], "height": f["height"], "status": status[k][0]})
    rows.sort(key=lambda r: r["lock_time"])
    return rows


# ------------------------------------------------------------------ fake-input check
def selftest():
    T = 1_800_000_000
    def rec(a_from, a_to, cf="XMR", ct="BTC", created=T - 1000, ttl=600, frf=5, addr="PabcdefXYZ"):
        return {"created": created, "cf": cf, "ct": ct, "a_from": a_from, "a_to": a_to, "amt_from": a_from / 1e12 if cf == "XMR" else a_from / 1e8,
                "amt_to": a_to / 1e8 if ct == "BTC" else a_to / 1e12, "rate": 1.0, "ttl": ttl, "lock_type": 2, "lock_value": 86400, "aa": 1, "pv": 8, "mn": None,
                "frf": frf, "frt": 5, "addr": addr}
    idx = {}
    merge_offer(idx, "m1", T - 900, T + 300, rec(10**12, 500_000))              # reverse: taker locks BTC = amount_to
    merge_offer(idx, "m2", T - 900, T - 4000, rec(10**12, 700_000))             # listed, but gone more than an hour before the lock
    merge_offer(idx, "m3", T + 900, T + 2000, rec(10**12, 900_000))             # first seen more than 10 min after the lock
    merge_offer(idx, "m4", T - 900, T + 300, rec(1_234_567, 10**12, cf="BTC", ct="XMR"))   # normal: maker locks its BTC = amount_from
    merge_offer(idx, "m5", T - 50_000, T - 40_000, rec(10**12, 1, created=T - 2000))        # same terms as m1, created 1000 s earlier: counted
    merge_offer(idx, "m6", T - 99_000, T - 98_000, rec(10**12, 2, created=T - 1000 - 90_000))  # same terms, created more than 24 h earlier: not counted
    merge_offer(idx, "m7", T - 50_000, T - 40_000, rec(10**12, 3, frf=6))       # different fee rate: not counted
    merge_offer(idx, "m1", T - 1200, T + 600, rec(10**12, 500_000))             # seen again in another source: first/last widen
    assert idx["m1"]["first_seen"] == T - 1200 and idx["m1"]["last_seen"] == T + 600
    px = {"BTC": ([T - 2000, T - 100], [60000.0, 61000.0]), "XMR": ([T - 2000, T - 100], [600.0, 610.0])}
    ev = [("BTC", "completed", 500_002, T, T + 1200, "tx1"), ("BTC", "completed", 700_000, T, T + 1200, "tx2"), ("BTC", "completed", 900_000, T, T + 1200, "tx3"),
          ("BTC", "failed", 1_234_565, T, T + 90_000, "tx4"), ("BTC", "completed", 500_003, T, T + 1200, "tx5")]
    rows = match(ev, idx, px)
    got = sorted((r["txid"], r["msg_id"], r["direction"], r["outcome"]) for r in rows)
    assert got == [("tx1", "m1", "reverse", "completed"), ("tx4", "m4", "normal", "failed")], got
    r1 = next(r for r in rows if r["txid"] == "tx1")
    # same terms within 24 h, counted as trap_labels.py does (the offer itself included): m1, m2 and m3 (same default terms and creation time), m5; not m6 or m7
    assert r1["same_terms_24h"] == 4 and r1["usd"] == round(0.005 * 61000.0, 2) and r1["addr"] == "Pabcde..." and r1["lock_to_spend_h"] == 0.33
    assert r1["prem_lock_pct"] == round(((610.0 / 61000.0) / (0.005 / 1.0) - 1) * 100, 2)
    assert usd(px, "BTC", T + 2000) is None                                    # last tick older than 15 minutes: no price
    assert days_between("20261130", "20261202") == ["20261130", "20261201", "20261202"]
    assert cv("amount_from", "12") == 12 and cv("amount_from", 12.0) == 12 and cv("rate", "1.5") == 1.5 and cv("coin_from", "XMR") == "XMR"
    # early lock flags: flagged rows only, first sighting kept, block height filled in later; offer-matched, unmatched flags kept
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "early.csv")
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["seen_utc", "stage", "coin", "txid", "vout", "value_sat", "height", "flag"])
            w.writerow([iso(T - 60), "mempool", "BTC", "aa", 0, 500_001, "", 1]); w.writerow([iso(T + 300), "block", "BTC", "aa", 0, 500_001, 870001, 1])
            w.writerow([iso(T), "mempool", "BTC", "bb", 1, 200_000, "", 0]); w.writerow([iso(T), "block", "LTC", "cc", 0, 777_777, 3215933, 1])
            w.writerow([iso(T - 9 * 86400), "block", "BTC", "old", 0, 500_001, 1, 1])
        fl = load_early(p, T - 86400)
        assert set(fl) == {("BTC", "aa", 0), ("LTC", "cc", 0)} and fl[("BTC", "aa", 0)]["stage"] == "mempool" and fl[("BTC", "aa", 0)]["height"] == "870001"
        st = {("BTC", "aa", 0): ("completed", T + 1500), ("LTC", "cc", 0): ("pending", None)}
        er = early_rows(fl, st, idx, px)
        a = next(r for r in er if r["txid"] == "aa")
        c = next(r for r in er if r["txid"] == "cc")
        assert a["msg_id"] == "m1" and a["direction"] == "reverse" and a["status"] == "completed" and a["lock_vout"] == 0 and a["first_stage"] == "mempool"
        assert c["status"] == "pending" and "msg_id" not in c and c["lock_sat"] == 777_777
        assert early_status({}, 0) == {} and early_status({("BTC", "x", 0): None}, 0, db_path="/nonexistent") == {("BTC", "x", 0): ("pending", None)}
    return True


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of-day", help="pretend the Parquet tables end on this day (test: compare with the nightly match)")
    ap.add_argument("--out-dir", help="write outputs and the cache here instead of data/analysis (test)")
    ap.add_argument("--nightly-csv", help="nightly match file to merge with (default: the newest data/analysis/trap_labels_YYYYMMDD.csv)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    selftest()
    if args.selftest:
        print("fake-input check passed"); return 0
    import duckdb
    t_start = time.time(); now = int(t_start)
    out_dir = args.out_dir or ANALYSIS
    cache_dir = os.path.join(out_dir, "offer_match_live")
    os.makedirs(cache_dir, exist_ok=True)
    lock = open(os.path.join(cache_dir, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("offer_match_live: another run holds the lock; nothing to do"); return 0

    pq_snap = sorted(os.path.basename(p)[5:] for p in glob.glob(os.path.join(PQ, "site_snapshots", "date=*")))
    pq_tick = sorted(os.path.basename(p)[5:] for p in glob.glob(os.path.join(PQ, "ticker", "date=*")))
    d_last = args.as_of_day or pq_snap[-1]
    t_last = args.as_of_day or (pq_tick[-1] if pq_tick else d_last)
    nightly = args.nightly_csv or (sorted(glob.glob(os.path.join(ANALYSIS, "trap_labels_2*.csv")))[-1:] or [None])[0]
    stamp = re.search(r"(\d{8})", os.path.basename(nightly)).group(1) if nightly else None
    cover_end = day_start(d_last) + 86400
    if stamp and not args.as_of_day:
        cover_end = min(cover_end, day_start(stamp))          # the nightly file covers what the Parquet tables held when it ran
    live_start = max(T0, cover_end - MARGIN_S)
    today = day_of(now)
    raw_days = [d for d in days_between(day_of(cover_end), today) if d > d_last]
    if len(raw_days) > MAX_LIVE_DAYS:
        log(f"offer_match_live: {len(raw_days)} raw days to rebuild (Parquet ends {d_last}); refusing, the nightly job needs fixing first"); return 2
    ctx_days = [d for d in pq_snap if day_of(live_start - (CTX_DAYS - 1) * 86400) <= d <= d_last]
    tick_pq = [d for d in pq_tick if day_of(live_start - (CTX_DAYS - 1) * 86400) <= d <= t_last]
    tick_raw = [d for d in days_between(day_of(day_start(t_last) + 86400), today)]

    con = duckdb.connect()
    con.execute("set threads=1; set memory_limit='250MB'")    # the unit caps the process at 500 MB
    idx, st = build_offers(con, ctx_days, raw_days, cache_dir, now)
    px = load_prices(con, tick_pq, tick_raw)
    con.close()
    events = load_events(live_start)
    rows = match(events, idx, px)
    flags = load_early(EARLY_CSV, now - EARLY_WINDOW_S)
    fstat = early_status(flags, now - EARLY_WINDOW_S - 86400)
    erows = early_rows(flags, fstat, idx, px)
    for f in glob.glob(os.path.join(cache_dir, "raw_offers_*.json")):          # raw days now in Parquet: the nightly job has them
        if re.search(r"(\d{8})", os.path.basename(f)).group(1) <= d_last and not args.as_of_day:
            os.unlink(f)

    def write_rows(fh, header, rs):
        w = csv.DictWriter(fh, fieldnames=header); w.writeheader(); w.writerows(rs)
    live_path = os.path.join(out_dir, "offer_match_live.csv")
    atomic_write(live_path, lambda fh: write_rows(fh, FIELDS, rows))
    atomic_write(os.path.join(out_dir, "offer_match_early.csv"), lambda fh: write_rows(fh, EARLY_FIELDS, [{k: r.get(k, "") for k in EARLY_FIELDS} for r in erows]))
    from collections import Counter
    early_counts = dict(Counter(fstat[k][0].split(":")[0] for k in flags))
    merged = None
    if nightly and os.path.exists(nightly):
        with open(nightly, newline="") as fh:
            rd = csv.DictReader(fh)
            header = rd.fieldnames or []
            old = [r for r in rd if r.get("spend_time") and int(float(r["spend_time"])) < live_start]
        if sorted(header) == sorted(FIELDS):
            merged = annotate(old + rows)                     # candidate counts across the nightly and live rows together
            latest = os.path.join(out_dir, "trap_labels_latest.csv")
            atomic_write(latest, lambda fh: write_rows(fh, header, merged), mode_from=latest if os.path.exists(latest) else None)
        else:
            log(f"offer_match_live: columns of {os.path.basename(nightly)} differ from this module's; trap_labels_latest.csv left alone")
    status = {"run_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)), "seconds": round(time.time() - t_start, 2),
              "live_start_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(live_start)), "parquet_offers_end": d_last, "parquet_ticker_end": t_last,
              "nightly_file": os.path.basename(nightly) if nightly else None, **st, "events_live": len(events),
              "events_matched": len({(r["txid"], r["outcome"]) for r in rows}), "rows_live": len(rows),
              "rows_in_trap_labels_latest": len(merged) if merged is not None else None, "as_of_day": args.as_of_day,
              "early_flags_48h": len(flags), "early_by_status": early_counts, "early_rows": len(erows)}
    atomic_write(os.path.join(out_dir, "offer_match_live_status.json"), lambda fh: json.dump(status, fh, indent=1))
    log("offer_match_live: " + json.dumps({k: status[k] for k in ("live_start_utc", "raw_files_new", "offers", "events_live", "events_matched", "rows_live",
                                                                     "rows_in_trap_labels_latest", "seconds")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
