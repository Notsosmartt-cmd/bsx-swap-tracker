"""Build the demo: a synthetic BasicSwap-style market and a static page (demo/index.html) that shows what the tracker produces.

Two kinds of data, never mixed up on the page:
  REAL aggregates   datasets/daily_swaps.csv and datasets/validation.json (daily counts per chain and the measured quality figures; no txids,
                    addresses or per-swap rows)
  SYNTHETIC market  datasets/synthetic/ written by this script with a fixed seed: makers, offers, swaps and early lock flags drawn from a few
                    calibrated parameters (lock-to-spend minutes p10/p50/p90 = 9.9 / 18.8 / 50.3, refund share 2.4 %, early-detection recall
                    72 % and precision 96.9 %); every maker, offer and swap in it is invented
Standard library only.  Run:  python3 demo/make_demo.py   (then open demo/index.html; no server needed)

Your own data:  python3 demo/make_demo.py --own <data_dir>/report [--early <data_dir>/offer_match_early.csv] [--out demo/own.html]
  builds the same page from `bsx-swap-tracker report` output (daily_swaps.csv, chains.csv, pairs.csv, sizes.csv, makers.csv) and, optionally, the
  early-flag file of `bsx-swap-tracker match`; nothing synthetic is shown in that mode. The page stays local: do not publish it if it holds makers.
"""
import argparse, csv, json, math, os, random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYN = os.path.join(ROOT, "datasets", "synthetic")
SEED, DAYS, T_END = 7, 60, 1790000000          # T_END: a fixed synthetic end time, so the output never changes between runs
PRICE = {"BTC": 60000.0, "LTC": 80.0, "XMR": 150.0, "PART": 0.5, "BCH": 400.0}   # synthetic USD prices, held constant
PAIRS = [("XMR", "BTC", 0.42), ("XMR", "LTC", 0.24), ("BTC", "XMR", 0.12), ("LTC", "XMR", 0.08), ("PART", "BTC", 0.06), ("BCH", "XMR", 0.05), ("PART", "LTC", 0.03)]
SCRIPT_CHAIN = {"BTC", "LTC", "PART", "BCH"}   # the chain the tracker sees the lock on: the scripted (non-Monero) leg
LOCK_MIN = (18.8, math.log(50.3 / 18.8) / 1.2816)   # lognormal median and sigma for lock -> spend minutes
REFUND, EARLY_RECALL, EARLY_PRECISION = 0.024, 0.72, 0.969


def gen():
    rnd = random.Random(SEED)
    makers = []
    for i in range(12):
        w = 1.0 / (i + 1) ** 1.1                                 # a few large makers, a long tail
        makers.append({"maker": f"M{i + 1:02d}", "weight": round(w, 4), "auto_accept": int(rnd.random() < 0.7),
                       "median_usd": round(math.exp(rnd.uniform(math.log(80), math.log(1500)))), "pairs": "|".join(
                           sorted({p[0] + "/" + p[1] for p in rnd.choices(PAIRS, weights=[p[2] for p in PAIRS], k=3)}))})
    offers, swaps, flags = [], [], []
    t0 = T_END - DAYS * 86400
    oid = 0
    for d in range(DAYS):
        for m in makers:
            n = rnd.choices(range(0, 30), k=1)[0] if m["weight"] > 0.3 else rnd.randint(0, int(40 * m["weight"]) + 2)
            for _ in range(n):
                oid += 1
                pair = rnd.choice(m["pairs"].split("|"))
                cf, ct = pair.split("/")
                usd = m["median_usd"] * math.exp(rnd.gauss(0, 0.8))
                created = t0 + d * 86400 + rnd.randint(0, 86399)
                ttl = rnd.choice([3600, 7200, 21600, 86400])
                premium = rnd.gauss(1.0, 0.8)                     # percent over the synthetic mid
                p_fill = min(0.9, 0.55 * math.exp(-usd / 900.0) * (1.4 if m["auto_accept"] else 0.6) * math.exp(-max(premium, 0) / 2.5))
                filled = rnd.random() < p_fill
                o = {"offer": f"O{oid:05d}", "maker": m["maker"], "pair": pair, "usd": round(usd, 2), "premium_pct": round(premium, 2),
                     "created": created, "ttl_s": ttl, "filled": int(filled)}
                offers.append(o)
                if not filled:
                    continue
                lock = created + rnd.randint(60, ttl)
                chain = cf if cf in SCRIPT_CHAIN else ct
                outcome = "refunded" if rnd.random() < REFUND else "completed"
                spend = lock + int(60 * LOCK_MIN[0] * math.exp(rnd.gauss(0, LOCK_MIN[1]))) if outcome == "completed" else lock + 2 * 3600 + rnd.randint(0, 7200)
                swaps.append({"swap": f"S{len(swaps) + 1:05d}", "offer": o["offer"], "maker": m["maker"], "pair": pair, "chain": chain,
                              "usd": o["usd"], "lock_time": lock, "spend_time": spend, "outcome": outcome})
                if chain == "BTC" and rnd.random() < EARLY_RECALL:
                    flags.append({"seen": lock + rnd.randint(1, 30), "chain": chain, "usd": o["usd"], "pair": pair, "maker": m["maker"],
                                  "status": outcome, "minutes_ahead": round((spend - lock) / 60.0, 1)})
    n_false = round(len(flags) * (1 - EARLY_PRECISION) / EARLY_PRECISION)
    for _ in range(n_false):                                       # flags that never became a swap (amount coincidences)
        flags.append({"seen": t0 + rnd.randint(0, DAYS * 86400), "chain": "BTC", "usd": round(math.exp(rnd.gauss(math.log(300), 0.9)), 2),
                      "pair": "XMR/BTC", "maker": rnd.choice(makers)["maker"], "status": "spent:not-a-swap", "minutes_ahead": ""})
    flags.sort(key=lambda r: r["seen"])
    return makers, offers, swaps, flags


def write_csv(name, rows):
    os.makedirs(SYN, exist_ok=True)
    with open(os.path.join(SYN, name), "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def summarise(makers, offers, swaps, flags):
    pairs = {}
    for s in swaps:
        p = pairs.setdefault(s["pair"], {"pair": s["pair"], "swaps": 0, "usd": 0.0, "refunded": 0, "sizes": []})
        p["swaps"] += 1; p["usd"] += s["usd"]; p["refunded"] += s["outcome"] == "refunded"; p["sizes"].append(s["usd"])
    for p in pairs.values():
        p["median_usd"] = round(sorted(p["sizes"])[len(p["sizes"]) // 2], 0); p["usd"] = round(p["usd"]); del p["sizes"]
    buckets = [(0, 100), (100, 250), (250, 500), (500, 1000), (1000, 2500), (2500, 1e9)]
    fill = []
    for lo, hi in buckets:
        b = [o for o in offers if lo <= o["usd"] < hi]
        fill.append({"bucket": f"{lo}-{int(hi) if hi < 1e9 else '+'}", "offers": len(b), "filled": sum(o["filled"] for o in b)})
    share = {}
    for s in swaps:
        share[s["maker"]] = share.get(s["maker"], 0) + s["usd"]
    lead = sorted(f["minutes_ahead"] for f in flags if f["minutes_ahead"] != "")
    daily = {}
    for s in swaps:
        d = (s["spend_time"] - (T_END - DAYS * 86400)) // 86400
        daily.setdefault(d, {"completed": 0, "refunded": 0})[s["outcome"]] += 1
    return {"pairs": sorted(pairs.values(), key=lambda p: -p["usd"]), "fill": fill,
            "makers": sorted(({"maker": k, "usd": round(v)} for k, v in share.items()), key=lambda r: -r["usd"]),
            "lead_p10_p50_p90": [lead[int(len(lead) * q)] for q in (0.1, 0.5, 0.9)] if lead else [],
            "flags": flags[-15:], "n": {"makers": len(makers), "offers": len(offers), "swaps": len(swaps), "flags": len(flags)},
            "daily": [{"day": d, **v} for d, v in sorted(daily.items()) if 0 <= d < DAYS]}


def read(d, name):
    p = os.path.join(d, name)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def report_data(d):
    """the aggregates of `bsx-swap-tracker report` (the shipped datasets are its --public output)"""
    daily = [{"day": r["day"], "chain": r["chain"], "completed": int(r["completed"]), "refunded": int(r["refunded"])} for r in read(d, "daily_swaps.csv")]
    return {"real_daily": daily, "chains": read(d, "chains.csv"), "pairs": read(d, "pairs.csv"), "sizes": read(d, "sizes.csv"), "makers": read(d, "makers.csv")[:15]}


def render(data, out):
    page = open(os.path.join(HERE, "template.html")).read().replace("/*DATA*/null", json.dumps(data, separators=(",", ":")))
    with open(out, "w") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--own", metavar="REPORT_DIR", help="build the page from your own `bsx-swap-tracker report` output instead of the shipped datasets")
    ap.add_argument("--early", metavar="CSV", help="with --own: offer_match_early.csv of `bsx-swap-tracker match`")
    ap.add_argument("--out", help="output page (default demo/index.html, or demo/own.html with --own)")
    a = ap.parse_args()
    if a.own:
        data = dict(report_data(a.own), mode="own", validation=None, synthetic=None,
                    early=[r for r in read(*os.path.split(a.early))][-15:] if a.early else [])
        out = a.out or os.path.join(HERE, "own.html")
        render(data, out)
        print(f"own data: {len(data['real_daily'])} chain-days, {len(data['pairs'])} pair rows -> {out}")
        return
    makers, offers, swaps, flags = gen()
    for name, rows in (("makers.csv", makers), ("offers.csv", offers), ("swaps.csv", swaps), ("early_locks.csv", flags)):
        write_csv(name, rows)
    data = dict(report_data(os.path.join(ROOT, "datasets")), mode="demo", validation=json.load(open(os.path.join(ROOT, "datasets", "validation.json"))),
                synthetic=summarise(makers, offers, swaps, flags), early=[])
    render(data, a.out or os.path.join(HERE, "index.html"))
    print(f"synthetic: {len(makers)} makers, {len(offers)} offers, {len(swaps)} swaps, {len(flags)} early flags -> datasets/synthetic/; demo/index.html written")


if __name__ == "__main__":
    main()
