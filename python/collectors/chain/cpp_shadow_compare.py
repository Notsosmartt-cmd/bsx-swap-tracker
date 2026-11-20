"""Shadow parity of the C++ swap tracker (docs/architecture.md, stages S2 / S4), 2026-11-19.

Compares the C++ shadow database (<data_dir>/cpp_shadow/chainscan.sqlite, written by `bin/bsx-swap-tracker scan --db ... --out-dir ...`)
with the production database (written by the Python scan) over the heights both hold, per coin and table:
  blocks, script_spends, swap_events (all columns but the row id), p2sh_outputs (within both retention windows).
The first --margin-days of the shadow range are skipped: there the shadow's script-hash index does not yet hold the lock outputs that production
knows, so lock data (lock_tx, amount, tier) can legitimately differ.  Rows are compared as exact tuples (hashed, so memory stays small).
Read-only towards both databases.  Writes <data_dir>/cpp_shadow/compare_latest.json and appends one line per coin to compare_history.csv
(the S4 exit criterion is 7 consecutive days with zero differences).

  python3 python/collectors/chain/cpp_shadow_compare.py [--margin-days 1.5] [--coins BTC,LTC,BCH,PART]
"""
import argparse, csv, hashlib, json, os, sqlite3, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
PROD = os.path.join(ROOT, "data", "analysis", "chainscan", "chainscan.sqlite")
SHADOW_DIR = os.path.join(ROOT, "data", "analysis", "chainscan", "cpp_shadow")
BPD = {"BTC": 144, "LTC": 576, "BCH": 144, "PART": 720}
TABLES = {
    "blocks": ("SELECT coin, height, hash, time, ntx, prev FROM blocks WHERE coin=? AND height BETWEEN ? AND ?", 2),
    "script_spends": ("SELECT coin, height, time, txid, vin, script_sha256, script_len, script_hex, nwit, nin, nout, prev_txid, prev_vout, prev_value "
                      "FROM script_spends WHERE coin=? AND height BETWEEN ? AND ?", 4),
    "swap_events": ("SELECT coin, height, block_hash, block_time, txid, vin, kind, protocol, confidence, secret_hash, secret, lock_val, script_sha256, amount_sat, "
                    "prev_txid, prev_vout, sequence, tx_nin, tx_nout, tx_version, tx_locktime, outputs, lock_tx, lock_height, lock_time, extra "
                    "FROM swap_events WHERE coin=? AND height BETWEEN ? AND ?", 5),
    "p2sh_outputs": ("SELECT coin, txid, vout, value, otype, program, height, time, txinfo FROM p2sh_outputs WHERE coin=? AND height BETWEEN ? AND ?", 2),
}


LOOKUP_COLS = {"script_spends": {13}, "swap_events": {8, 13, 22, 23, 24, 25}}   # prev_value; confidence, amount_sat, lock_tx, lock_height, lock_time, extra


def older_than_shadow(pr, sh, coin, table, row, shadow_first, shadow_complete):
    """the spent output was created before the shadow's first stored block: production's index says so, or (shadow index complete since its first
    block, no gaps, nothing pruned yet) the shadow's index does not hold it although the spend's other columns agree"""
    if table == "swap_events":
        return row[23] is not None and row[23] < shadow_first
    r = pr.execute("SELECT height FROM p2sh_outputs WHERE coin=? AND txid=? AND vout=?", (coin, row[11], row[12])).fetchone()
    if r is not None:
        return r[0] < shadow_first
    q = sh.execute("SELECT height FROM p2sh_outputs WHERE coin=? AND txid=? AND vout=?", (coin, row[11], row[12])).fetchone()
    if q is not None:
        return q[0] < shadow_first
    return shadow_complete


def rows(db, sql, coin, lo, hi, key_cols):
    out = {}
    for r in db.execute(sql, (coin, lo, hi)):
        k = tuple(r[:key_cols + 1])[1:] if key_cols else ()
        out[k if key_cols else r] = hashlib.sha1(repr(r).encode()).digest()
    return out


def keyed(db, sql, coin, lo, hi, table):
    keys = {"blocks": (1,), "script_spends": (3, 4), "swap_events": (4, 5, 6), "p2sh_outputs": (1, 2)}[table]
    out = {}
    for r in db.execute(sql, (coin, lo, hi)):
        out[tuple(r[i] for i in keys)] = (hashlib.sha1(repr(tuple(r)).encode()).digest(), r)
    return out


def compare_watch():
    """Block-stage early-lock rows of the Python watch (production) and the C++ shadow watch, over the time both ran.  Keys (coin, txid, vout) and
    the flag must match; mempool rows are only counted (different peers relay different transactions)."""
    import datetime as dt
    def load(path):
        out = []
        try:
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        r["_t"] = dt.datetime.fromisoformat(r["seen_utc"].replace("Z", "+00:00")).timestamp()
                    except (KeyError, ValueError):
                        continue
                    out.append(r)
        except OSError:
            pass
        return out
    py = load(os.path.join(ROOT, "data", "analysis", "chainscan", "early_locks.csv"))
    cp = load(os.path.join(SHADOW_DIR, "early_locks.csv"))
    if not cp or not py:
        return {"note": "no rows yet in one of the files", "rows_python": len(py), "rows_cpp": len(cp)}
    lo, hi = max(min(r["_t"] for r in cp), min(r["_t"] for r in py)), min(max(r["_t"] for r in cp), max(r["_t"] for r in py)) - 600
    sel = lambda rows, st: {(r["coin"], r["txid"], r["vout"]): r["flag"] for r in rows if r["stage"] == st and lo <= r["_t"] <= hi}
    pb, cb = sel(py, "block"), sel(cp, "block")
    pm, cm = sel(py, "mempool"), sel(cp, "mempool")
    only_p, only_c = sorted(set(pb) - set(cb)), sorted(set(cb) - set(pb))
    flag_diff = [k for k in pb if k in cb and pb[k] != cb[k]]
    return {"window_utc": [time.strftime("%Y-%m-%d %H:%M", time.gmtime(lo)), time.strftime("%Y-%m-%d %H:%M", time.gmtime(hi))],
            "block_rows_python": len(pb), "block_rows_cpp": len(cb), "block_only_python": len(only_p), "block_only_cpp": len(only_c), "block_flag_differences": len(flag_diff),
            "block_differences": len(only_p) + len(only_c) + len(flag_diff), "examples": [list(x) for x in (only_p[:2] + only_c[:2] + flag_diff[:2])],
            "mempool_rows_python": len(pm), "mempool_rows_cpp": len(cm), "mempool_common": len(set(pm) & set(cm))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", default=os.path.join(SHADOW_DIR, "chainscan.sqlite"))
    ap.add_argument("--prod", default=PROD)
    ap.add_argument("--coins", default="BTC,LTC,BCH,PART")
    ap.add_argument("--margin-days", type=float, default=1.5)
    ap.add_argument("--watch", action="store_true", help="also compare the block-stage early-lock flags of the two watchers")
    a = ap.parse_args()
    sh = sqlite3.connect(f"file:{a.shadow}?mode=ro", uri=True, timeout=60)
    pr = sqlite3.connect(f"file:{a.prod}?mode=ro", uri=True, timeout=60)
    report = {"run_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "margin_days": a.margin_days, "coins": {}}
    total_diff = 0
    for coin in a.coins.split(","):
        s_hi = sh.execute("SELECT MAX(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
        first = sh.execute("SELECT height FROM blocks WHERE coin=? ORDER BY rowid LIMIT 1", (coin,)).fetchone()
        s_lo = first[0] if first else None                    # the first block the shadow scanned FORWARD: its lookups only knew outputs from here on
        p_lo, p_hi = pr.execute("SELECT MIN(height), MAX(height) FROM blocks WHERE coin=?", (coin,)).fetchone()
        if s_lo is None or p_lo is None:
            report["coins"][coin] = {"note": "no blocks in one of the databases"}
            continue
        lo, hi = max(s_lo + int(a.margin_days * BPD[coin]), p_lo), min(s_hi, p_hi)
        if lo > hi:
            report["coins"][coin] = {"note": "no overlap after the margin", "shadow": [s_lo, s_hi], "prod": [p_lo, p_hi]}
            continue
        n_sh, gaps_sh = sh.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM gaps WHERE coin=?) FROM blocks WHERE coin=? AND height BETWEEN ? AND ?", (coin, coin, s_lo, s_hi)).fetchone()
        s_complete = n_sh == s_hi - s_lo + 1 and gaps_sh == 0 and (s_hi - s_lo) < 7 * BPD[coin]     # every block since the first, nothing pruned
        c = {"heights": [lo, hi], "blocks_compared": hi - lo + 1, "shadow_index_complete": s_complete, "tables": {}}
        t_lo, t_hi = sh.execute("SELECT MIN(time), MAX(time) FROM blocks WHERE coin=? AND height BETWEEN ? AND ?", (coin, lo, hi)).fetchone()
        for t, (sql, _) in TABLES.items():
            A = keyed(sh, sql, coin, lo, hi, t)
            if t == "script_spends":
                # production has no height index on script_spends (6.9 M BTC rows: a height scan reads GBs and pushed IO pressure on the protected
                # services to 64 % on 2026-11-19 22:58 UTC); look up the shadow's rows by primary key instead. Rows only production has are not
                # detected here; identical blocks (hash, tx count) and corpus parity cover that side.
                B = {}
                cols = sql.split("SELECT ", 1)[1].split(" FROM", 1)[0]
                for k in A:
                    r = pr.execute("SELECT %s FROM script_spends WHERE coin=? AND txid=? AND vin=?" % cols, (coin, k[0], k[1])).fetchone()
                    if r is not None:
                        B[k] = (hashlib.sha1(repr(tuple(r)).encode()).digest(), r)
            elif t == "swap_events":
                # through the (coin, block_time) index, then the height range in Python
                q = sql.replace("WHERE coin=? AND height BETWEEN ? AND ?", "WHERE coin=? AND block_time BETWEEN ? AND ?")
                B = {k: v for k, v in keyed(pr, q, coin, t_lo, t_hi, t).items() if lo <= v[1][1] <= hi}
            else:
                B = keyed(pr, sql, coin, lo, hi, t)
            only_s = [k for k in A if k not in B]
            only_p = [k for k in B if k not in A]
            diff_all = [k for k in A if k in B and A[k][0] != B[k][0]]
            # expected: only the columns that depend on the script-hash index lookup differ, the shadow knows nothing there, and production shows the
            # spent output was created before the shadow's first block (so the shadow cannot know it)
            diff, expected = [], 0
            for k in diff_all:
                ra, rb = A[k][1], B[k][1]
                cols = {i for i in range(len(ra)) if ra[i] != rb[i]}
                if t in LOOKUP_COLS and cols <= LOOKUP_COLS[t] and older_than_shadow(pr, sh, coin, t, rb, s_lo, s_complete):
                    expected += 1
                else:
                    diff.append(k)
            if t == "script_spends":
                only_p = []                                       # not checked (see above)
            d = {"rows_shadow": len(A), "rows_prod": len(B), "identical": len(A) - len(only_s) - len(diff_all), "expected_lookup_gap": expected,
                 "different": len(diff), "only_shadow": len(only_s), "only_prod": len(only_p)}
            if diff:
                k = diff[0]
                d["example_diff"] = {"key": list(map(str, k)), "shadow": [str(x)[:120] for x in A[k][1]], "prod": [str(x)[:120] for x in B[k][1]]}
            if only_s or only_p:
                d["example_missing"] = {"only_shadow": [list(map(str, x)) for x in only_s[:2]], "only_prod": [list(map(str, x)) for x in only_p[:2]]}
            c["tables"][t] = d
            total_diff += len(diff) + len(only_s) + len(only_p)
        report["coins"][coin] = c
    if a.watch:
        report["watch"] = compare_watch()
        total_diff += report["watch"].get("block_differences", 0)
    report["total_differences"] = total_diff
    os.makedirs(SHADOW_DIR, exist_ok=True)
    with open(os.path.join(SHADOW_DIR, "compare_latest.json"), "w") as f:
        json.dump(report, f, indent=1)
    hist = os.path.join(SHADOW_DIR, "compare_history.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_utc", "coin", "from_height", "to_height", "table", "rows", "identical", "different", "only_shadow", "only_prod"])
        for coin, c in report["coins"].items():
            for t, d in (c.get("tables") or {}).items():
                w.writerow([report["run_utc"], coin, c["heights"][0], c["heights"][1], t, d["rows_shadow"], d["identical"], d["different"], d["only_shadow"], d["only_prod"]])
    for coin, c in report["coins"].items():
        if "tables" not in c:
            print(coin, c.get("note"))
            continue
        print("%-4s heights %d-%d (%d blocks): %s" % (coin, c["heights"][0], c["heights"][1], c["blocks_compared"],
              "; ".join("%s %d/%d identical%s%s" % (t, d["identical"], d["rows_shadow"], (" (+%d lock older than the shadow start)" % d["expected_lookup_gap"]) if d.get("expected_lookup_gap") else "", (", %d diff, %d only C++, %d only Python" % (d["different"], d["only_shadow"], d["only_prod"]))
                                                   if d["different"] or d["only_shadow"] or d["only_prod"] else "") for t, d in c["tables"].items())))
    if a.watch:
        print("watch:", json.dumps(report["watch"]))
    print("total differences:", total_diff)
    return 0 if total_diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
