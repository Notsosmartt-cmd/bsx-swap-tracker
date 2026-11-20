"""Turn a scan's output into a small sample folder people can read without running anything.

    python3 samples/make_sample.py --out samples/2026-11-20            # from data/analysis/chainscan in this checkout
    python3 samples/make_sample.py --data-dir DIR --out samples/NAME

Writes trades.csv (every completed and refunded swap the scan found, one row each, verifiable on any block explorer),
event_counts.csv (every recognised event per tier, including the noise), report/ (the output of `bsx-swap-tracker report`),
watch_status.json (the live watcher's last status, peer addresses removed) and scan_summary.txt. Standard library only.
"""
import argparse, csv, json, os, re, shutil, sqlite3, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DECIMALS = {"BTC": 8, "LTC": 8, "PART": 8, "BCH": 8}


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "analysis", "chainscan"))
    ap.add_argument("--logs-dir", default=os.path.join(ROOT, "data", "logs"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    db_path = os.path.join(a.data_dir, "chainscan.sqlite")
    if not os.path.exists(db_path):
        sys.exit(f"no database at {db_path}: run `bin/bsx-swap-tracker scan` first")
    os.makedirs(a.out, exist_ok=True)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # completed swaps (top-tier lock spends) and failed ones (refund transactions), oldest first
    rows = db.execute("""select coin, kind, height, block_time, txid, amount_sat, lock_time, lock_height, prev_txid
                         from swap_events
                         where (kind = 'adaptor_lock_spend' and confidence = 'strong') or (kind = 'adaptor_refund_tx' and confidence = 'certain')
                         order by block_time, coin, txid""").fetchall()
    with open(os.path.join(a.out, "trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "outcome", "settled_utc", "locked_utc", "lock_to_settle_min", "amount", "amount_sat", "height", "txid", "lock_txid"])
        for coin, kind, height, bt, txid, amt, lt, lh, prev in rows:
            mins = round((bt - lt) / 60, 1) if (lt and bt and bt >= lt) else ""
            amount = f"{amt / 10 ** DECIMALS.get(coin, 8):.8f}" if amt is not None else ""
            w.writerow([coin, "completed" if kind == "adaptor_lock_spend" else "refunded", utc(bt), utc(lt), mins, amount,
                        amt if amt is not None else "", height, txid, prev or ""])

    with open(os.path.join(a.out, "event_counts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "kind", "tier", "events", "counted_by_report"])
        for coin, kind, conf, n in db.execute("select coin, kind, confidence, count(*) from swap_events group by 1, 2, 3 order by 1, 4 desc"):
            counted = (kind == "adaptor_lock_spend" and conf == "strong") or (kind == "adaptor_refund_tx" and conf == "certain")
            w.writerow([coin, kind, conf, n, "yes" if counted else "no"])

    rep = os.path.join(a.data_dir, "report")
    if os.path.isdir(rep):
        os.makedirs(os.path.join(a.out, "report"), exist_ok=True)
        for name in ("daily_swaps.csv", "chains.csv", "sizes.csv"):
            if os.path.exists(os.path.join(rep, name)):
                shutil.copy(os.path.join(rep, name), os.path.join(a.out, "report", name))

    st_path = os.path.join(a.data_dir, "chainwatch_status.json")
    if os.path.exists(st_path):
        st = json.load(open(st_path))
        for c in st.get("coins", {}).values():
            c.pop("peer", None)
        st.pop("pid", None)
        with open(os.path.join(a.out, "watch_status.json"), "w") as f:
            json.dump(st, f, indent=1)

    lines = []
    log = os.path.join(a.logs_dir, "chain_swap_scan.log")
    if os.path.exists(log):
        for ln in open(log, encoding="utf8", errors="replace"):
            if re.search(r"scanned \d+ blocks this run|run finished|synced headers to height", ln):
                lines.append(re.sub(r"\b(\d{1,3}\.){3}\d{1,3}\b", "<peer>", ln.rstrip()))
    with open(os.path.join(a.out, "scan_summary.txt"), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"{len(rows)} trades -> {a.out}")


if __name__ == "__main__":
    main()
