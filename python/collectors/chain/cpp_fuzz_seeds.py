"""Seed inputs for the C++ libFuzzer targets (cpp/fuzz), 2026-11-19.  Writes <data_dir>/fuzz/seeds/<target>/
from the golden corpus (public-chain blocks): blocks and single transactions with a leading family byte, revealed scripts, JSON samples and P2P
messages for every network magic.  Nothing is committed (data/ is private and git-ignored).
  python3 python/collectors/chain/cpp_fuzz_seeds.py"""
import glob, hashlib, json, os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import _paths  # noqa: E401,E402
import chainscan_lib as L  # noqa: E402
from collector_settings import S as SETTINGS  # noqa: E402

CORPUS = os.environ.get("BSX_CORPUS_DIR") or os.path.join(SETTINGS.data_dir, "corpus")   # public repo: fixtures/corpus
OUT = os.path.join(os.environ.get("BSX_FUZZ_DIR") or os.path.join(SETTINGS.data_dir, "fuzz"), "seeds")
FAM = {"BTC": (0, "btc"), "LTC": (1, "ltc"), "BCH": (2, "bch"), "PART": (3, "part")}
MAGIC = {"BTC": bytes.fromhex("f9beb4d9"), "LTC": bytes.fromhex("fbc0b6db"), "PART": bytes.fromhex("fbf2efb4")}


def put(target, data):
    d = os.path.join(OUT, target)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, hashlib.sha1(data).hexdigest()), "wb") as f:
        f.write(data)


def msg(magic, cmd, payload):
    return magic + cmd.encode().ljust(12, b"\0") + struct.pack("<I", len(payload)) + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] + payload


def main():
    n = {"block": 0, "tx": 0, "script": 0, "json": 0, "frame": 0}
    for path in sorted(glob.glob(os.path.join(CORPUS, "*", "*.bin"))):
        coin = os.path.basename(os.path.dirname(path))
        fb, fam = FAM[coin]
        raw = open(path, "rb").read()
        if len(raw) <= 600_000:
            put("block", bytes([fb]) + raw); n["block"] += 1
        r = L.Reader(raw); r.read(112 if fam == "part" else 80)
        k = r.varint()
        for i in range(k):
            a = r.pos
            tx = L.parse_part_tx(r) if (fam == "part" and r.buf[r.pos] == 0xA0) else L.parse_tx(r, "btc" if fam == "part" else fam)
            if i < 200 or any(len(v["witness"]) >= 2 for v in tx["vin"]):
                put("tx", bytes([fb]) + raw[a:r.pos]); n["tx"] += 1
            for v in tx["vin"]:
                if len(v["witness"]) >= 2 and 34 <= len(v["witness"][-1]) <= 1000 and n["script"] < 3000:
                    st = v["witness"][0][:255]
                    put("script", bytes([len(st)]) + st + v["witness"][-1]); n["script"] += 1
        if coin in MAGIC and len(raw) < 600_000:
            put("frame", msg(MAGIC[coin], "block", raw) + msg(MAGIC[coin], "ping", b"12345678")); n["frame"] += 1
    for coin, m in MAGIC.items():
        inv = b"\x02" + struct.pack("<I", 1) + b"\x11" * 32 + struct.pack("<I", 0x40000002) + b"\x22" * 32
        hdrs = b"\x01" + b"\x33" * (112 if coin == "PART" else 80) + b"\x00"
        put("frame", msg(m, "inv", inv) + msg(m, "headers", hdrs)[:-7]); put("frame", msg(m, "verack", b"") + msg(m, "headers", hdrs)); n["frame"] += 2
    for p in ("status.json", "chainwatch_status.json", "early_locks_latest.json", "headers_PART.json"):
        f = os.path.join(SETTINGS.data_dir, p)
        if os.path.exists(f):
            put("json", open(f, "rb").read()[:200_000]); n["json"] += 1
    for s in ('{"result":"00ab","error":null,"id":"chainscan"}', '{"result":null,"error":{"code":-28,"message":"Loading block index..."},"id":1}',
              '{"result":978086,"error":null}', '[1,2.5e10,-3,"\\u00e9\\ud83d\\ude00",true,false,null,{"a":[{}]}]'):
        put("json", s.encode()); n["json"] += 1
    print("seeds written:", n, "->", OUT)


if __name__ == "__main__":
    main()
