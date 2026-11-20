"""Golden block corpus for the C++ swap tracker (docs/architecture.md), 2026-11-19.

Builds <data_dir>/corpus/<COIN>/<height>.bin (raw blocks, as the sources deliver them) and <height>.txt (the canonical dump of the
Python parser = the reference).  Blocks are chosen per coin to cover every event kind the scan records (strong, likely and candidate adaptor
spends, refunds, BCH covenants, PART secret-hash redeems, PART RingCT outputs and anon inputs) plus the newest blocks.  Sources are the scan's own
verified paths: BTC/LTC from network peers checked against the header chain, PART from the local particld P2P port, BCH from the local daemon
through the read-only allowlist.

Canonical dump (one line each, the C++ `bsx-swap-tracker dump` must print exactly the same):
  block <hash> fam=<family> txs=<n> merkle=<ok|BAD>
  tx <txid> ver=<int> lt=<int> cb=<0|1>
  in <prev txid|-> <vout> <sequence> <scriptSig hex|-> w=<item hex,...|->
  out <value|-> <type> <script hex|->
  ev <kind> <tier> <txid> <vin> <amount|->                     (scan_block events without the output index; compared from stage S1b on)

  python3 python/collectors/chain/cpp_corpus.py --build [--coins BTC,LTC,BCH,PART]
  python3 python/collectors/chain/cpp_corpus.py --compare bin/bsx-swap-tracker [--events]
"""
import argparse, os, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import _paths  # noqa: E401,E402
import chainscan_lib as L  # noqa: E402
from collector_common import ROOT  # noqa: E402

CORPUS = os.path.join(ROOT, "data", "analysis", "chainscan", "corpus")
DB = os.path.join(ROOT, "data", "analysis", "chainscan", "chainscan.sqlite")
FAMILY = {"BTC": "btc", "LTC": "ltc", "BCH": "bch", "PART": "part"}
HDR = {"part": 112}


def hx(b):
    return b.hex() if b else "-"


def dump(raw, family, events=False):
    """Canonical text of one raw block (the reference the C++ parser is held to)."""
    hl = HDR.get(family, 80)
    header, txs = L.parse_raw_block(raw, family)
    from p2p_blocks import merkle_root
    ok = merkle_root([t["txid"] for t in txs]) == raw[36:68]
    out = ["block %s fam=%s txs=%d merkle=%s" % (L.dsha256(raw[:hl])[::-1].hex(), family, len(txs), "ok" if ok else "BAD")]
    for t in txs:
        out.append("tx %s ver=%d lt=%d cb=%d" % (t["txid"], t["version"], t["locktime"], 1 if t["coinbase"] else 0))
        for v in t["vin"]:
            out.append("in %s %d %d %s w=%s" % (v["txid"] or "-", v["vout"], v["sequence"], hx(v["script_sig"]),
                                                ",".join(w.hex() for w in v["witness"]) if v["witness"] else "-"))
        for o in t["vout"]:
            out.append("out %s %s %s" % ("-" if o["value"] is None else o["value"], L.out_type(o["script"])[0], hx(o["script"])))
    if events:
        evs = L.scan_block("X", family, 0, "00" * 32, header, txs, lambda a, b: None, wide=True)[2]   # no output index: lock data unknown
        for e in evs:
            out.append("ev %s %s %s %d %s" % (e["kind"], e["confidence"], e["txid"], e["vin"], "-" if e.get("amount_sat") is None else e["amount_sat"]))
    return "\n".join(out) + "\n"


def pick_heights(coin):
    import sqlite3
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    q = lambda sql, *a: [r[0] for r in db.execute(sql, a).fetchall() if r[0] is not None]
    hs = set()
    for kind, conf in (("adaptor_lock_spend", "strong"), ("adaptor_lock_spend", "likely"), ("adaptor_lock_spend", "candidate"), ("adaptor_refund_tx", None),
                       ("adaptor_refund_spend", None), ("bch_covenant_spend", None), ("htlc_redeem", None), ("htlc_refund", None)):
        if conf:
            hs.update(q("select max(height) from swap_events where coin=? and kind=? and confidence=?", coin, kind, conf))
        else:
            hs.update(q("select max(height) from swap_events where coin=? and kind=?", coin, kind))
    tip = q("select max(height) from blocks where coin=?", coin)
    if tip:
        hs.update({tip[0], tip[0] - 1})
    if coin == "PART":
        hs.update({2158321, 2158570})                      # RingCT outputs and anon inputs (the blocks that first broke the parser)
    db.close()
    return sorted(hs)


def fetch_raw(coin, heights):
    """height -> raw block bytes from the scan's verified sources."""
    out = {}
    if coin in ("BTC", "LTC"):
        import p2p_blocks as P
        src = P.P2PSource(coin, workers=0)
        for h in heights:
            if h <= src.chain.first or h > src.chain.tip:
                print("  %s %d outside the header window, skipped" % (coin, h)); continue
            raw = src._fetch_single(src.chain.hash(h), h)
            if P.dsha(raw[:80]) != src.chain.hash(h):
                raise SystemExit("%s %d: block does not match its header" % (coin, h))
            out[h] = raw
        src.close()
    elif coin == "PART":
        import part_p2p as PP
        src = PP.PartSource()
        for h in heights:
            want = src.chain.hash(h)
            raw = src.peer.getblock(want)
            PP.check_block(raw, want)
            out[h] = raw
        src.close()
    elif coin == "BCH":
        import chain_swap_scan as CS
        host, port, user, pw = CS.load_credentials("BCH")
        rpc = CS.ChainRPC("BCH", host, port, user, pw, timeout=120)
        for h in heights:
            out[h] = bytes.fromhex(rpc.call("getblock", [rpc.call("getblockhash", [h]), 0]))
    return out


def build(coins):
    for coin in coins:
        d = os.path.join(CORPUS, coin)
        os.makedirs(d, exist_ok=True)
        hs = pick_heights(coin)
        print(coin, "heights", hs)
        for h, raw in fetch_raw(coin, hs).items():
            with open(os.path.join(d, "%d.bin" % h), "wb") as f:
                f.write(raw)
            with open(os.path.join(d, "%d.txt" % h), "w") as f:
                f.write(dump(raw, FAMILY[coin]))
            with open(os.path.join(d, "%d.events.txt" % h), "w") as f:
                f.write("".join(ln + "\n" for ln in dump(raw, FAMILY[coin], events=True).splitlines() if ln.startswith("ev ")))
            print("  %s %d: %d bytes" % (coin, h, len(raw)))


def regolden():
    """Rewrite the .txt / .events.txt goldens from the stored .bin files (after a rule change in the Python reference)."""
    for coin in sorted(os.listdir(CORPUS)):
        d = os.path.join(CORPUS, coin)
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".bin")):
            raw = open(os.path.join(d, fn), "rb").read()
            with open(os.path.join(d, fn[:-4] + ".txt"), "w") as f:
                f.write(dump(raw, FAMILY[coin]))
            with open(os.path.join(d, fn[:-4] + ".events.txt"), "w") as f:
                f.write("".join(ln + "\n" for ln in dump(raw, FAMILY[coin], events=True).splitlines() if ln.startswith("ev ")))
    print("goldens rewritten")


def compare(binary, events=False):
    n = bad = 0
    t_py = t_cpp = 0.0
    for coin in sorted(os.listdir(CORPUS)):
        d = os.path.join(CORPUS, coin)
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".bin")):
            p = os.path.join(d, fn)
            want = open(p[:-4] + (".events.txt" if events else ".txt")).read()
            t0 = time.perf_counter()
            dump(open(p, "rb").read(), FAMILY[coin], events=events)
            t_py += time.perf_counter() - t0
            t0 = time.perf_counter()
            got = subprocess.run([binary, "events" if events else "dump", FAMILY[coin], p], capture_output=True, text=True)
            t_cpp += time.perf_counter() - t0
            n += 1
            if got.returncode or got.stdout != want:
                bad += 1
                a, b = want.splitlines(), got.stdout.splitlines()
                i = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
                print("MISMATCH %s/%s line %d\n  python: %s\n  c++   : %s\n  stderr: %s" % (coin, fn, i + 1, a[i][:160] if i < len(a) else "<end>",
                                                                                     b[i][:160] if i < len(b) else "<end>", got.stderr.strip()[:200]))
    print("compared %d blocks: %d identical, %d different; parse+dump time python %.2f s, c++ %.2f s (incl. process start)" % (n, n - bad, bad, t_py, t_cpp))
    return 1 if bad else 0


def main():
    global CORPUS
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--coins", default="BTC,LTC,BCH,PART")
    ap.add_argument("--compare", metavar="BINARY")
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--regolden", action="store_true")
    ap.add_argument("--corpus", default=CORPUS, help="corpus folder (<COIN>/<height>.bin|.txt|.events.txt); the public repository uses fixtures/corpus")
    a = ap.parse_args()
    CORPUS = a.corpus
    if a.build:
        build(a.coins.split(","))
    if a.regolden:
        regolden()
    if a.compare:
        return compare(a.compare, a.events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
