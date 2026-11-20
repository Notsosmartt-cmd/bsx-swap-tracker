"""Tests for the on-chain swap scan.  Run:  python3 python/collectors/tests/test_chainscan.py
Templates are rebuilt with the REAL BasicSwap builders when the node venv is present (venv/node/bin/python runs the whole file with them),
otherwise with byte strings written from the documented layout.  No network except a throw-away loopback server on a random port."""
import hashlib
import http.server
import json
import os
import sqlite3
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # python/collectors/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "basicswap"))
import chainscan_lib as L  # noqa: E402
import chain_swap_scan as S  # noqa: E402

REAL = False
try:
    from basicswap.protocols import atomic_swap_1  # noqa: E402
    from basicswap.interface.btc.btc import BTCInterface  # noqa: E402
    REAL = True
except Exception:       # missing pycryptodome etc. in this venv
    pass

KAL, KAF, KAS = b"\x02" + b"\x11" * 32, b"\x03" + b"\x22" * 32, b"\x02" + b"\x33" * 32
SECRET = b"\x55" * 32
SHASH = hashlib.sha256(SECRET).digest()
PKH1, PKH2 = b"\xaa" * 20, b"\xbb" * 20


def htlc_script(lock=100, op_lock=0xB2, op_hash=0xA8):
    if REAL:
        from basicswap.script import OpCodes
        lk = OpCodes.OP_CHECKSEQUENCEVERIFY if op_lock == 0xB2 else OpCodes.OP_CHECKLOCKTIMEVERIFY
        hs = OpCodes.OP_SHA256 if op_hash == 0xA8 else OpCodes.OP_SHA256_DECRED
        return bytes(atomic_swap_1.buildContractScript(lock, SHASH, PKH1, PKH2, op_lock=lk, op_hash=hs))
    num = bytes([1, lock]) if lock > 16 else bytes([0x50 + lock])
    return (bytes([0x63, 0x82, 0x01, 0x20, 0x88, op_hash, 0x20]) + SHASH + bytes([0x88, 0x76, 0xA9, 0x14]) + PKH1 + bytes([0x67]) + num
            + bytes([op_lock, 0x75, 0x76, 0xA9, 0x14]) + PKH2 + bytes([0x68, 0x88, 0xAC]))


def adaptor_lock_script():
    if REAL:
        return bytes(BTCInterface.genScriptLockTxScript(None, None, KAL, KAF)) if hasattr(BTCInterface, "genScriptLockTxScript") else b"\x52\x21" + KAL + b"\x21" + KAF + b"\x52\xae"
    return b"\x52\x21" + KAL + b"\x21" + KAF + b"\x52\xae"


def adaptor_refund_script(csv=10):
    if REAL:
        return bytes(BTCInterface.genScriptLockRefundTxScript(None, KAL, KAF, csv))
    num = bytes([0x50 + csv]) if csv <= 16 else bytes([1, csv])
    return b"\x63\x51\xb2\x75\x52\x21" + KAL + b"\x21" + KAF + b"\x52\xae\x67" + num + b"\xb2\x75\x21" + KAS + b"\xac\x68"


def push(b):
    n = len(b)
    if n == 0:
        return b"\x00"
    if n <= 75:
        return bytes([n]) + b
    if n <= 255:
        return b"\x4c" + bytes([n]) + b
    return b"\x4d" + struct.pack("<H", n) + b


def p2wsh(script):
    return b"\x00\x20" + hashlib.sha256(script).digest()


def p2sh(script):
    return b"\xa9\x14" + hashlib.new("ripemd160", hashlib.sha256(script).digest()).digest() + b"\x87"


def p2wpkh(h):
    return b"\x00\x14" + h


def varint(n):
    return bytes([n]) if n < 0xFD else b"\xfd" + struct.pack("<H", n)


def ser_tx(version, vin, vout, locktime=0, witness=None, extra=None):
    """vin: [(txid_hex, vout, script_sig, seq)], vout: [(value, script)], witness: [[items]] per input or None"""
    segwit = witness is not None
    b = struct.pack("<i", version) + (b"\x00\x01" if segwit else b"")
    b += varint(len(vin))
    for t, n, ss, seq in vin:
        b += bytes.fromhex(t)[::-1] + struct.pack("<I", n) + varint(len(ss)) + ss + struct.pack("<I", seq)
    b += varint(len(vout))
    for v, s in vout:
        b += struct.pack("<Q", v) + varint(len(s)) + s
    if segwit:
        for items in witness:
            b += varint(len(items)) + b"".join(varint(len(i)) + i for i in items)
    b += struct.pack("<I", locktime)
    if extra is not None:
        b += varint(len(extra)) + extra
    return b


def header(version=4, prev="00" * 32, time=1_780_000_000):
    return struct.pack("<i", version) + bytes.fromhex(prev)[::-1] + b"\x00" * 32 + struct.pack("<I", time) + b"\x00" * 8


def block(txs_raw, version=4, auxpow=b"", time=1_780_000_000):
    return header(version, time=time) + auxpow + varint(len(txs_raw)) + b"".join(txs_raw)


def cb_tx():
    return ser_tx(1, [("00" * 32, 0xFFFFFFFF, b"\x03\x01\x02\x03", 0xFFFFFFFF)], [(5000000000, b"\x51")])


def norm(raw, family="btc"):
    return L.parse_tx(L.Reader(raw), family)


class Recognition(unittest.TestCase):
    def run_block(self, family, raws, prior_outputs=None):
        hdr, txs = L.parse_raw_block(block(raws), family)
        idx = {}
        p2, _, _ = L.scan_block("T", family, 100, "h" * 64, hdr, txs, lambda a, b: None, wide=False, index_only=True)
        for r in p2:
            idx[(r[1], r[2])] = {"value": r[3], "otype": r[4], "program": r[5], "height": r[6], "time": r[7], "info": json.loads(r[8])}
        idx.update(prior_outputs or {})
        return L.scan_block("T", family, 100, "h" * 64, hdr, txs, lambda a, b: idx.get((a, b)), wide=True)

    def test_templates_match_builders(self):
        for op_lock in (0xB2, 0xB1):
            for op_hash in (0xA8, 0xC0):
                h = L.parse_htlc(htlc_script(100, op_lock, op_hash))
                self.assertIsNotNone(h, (op_lock, op_hash))
                self.assertEqual((h["secret_hash"], h["pkh_redeem"], h["pkh_refund"], h["lock_val"]), (SHASH, PKH1, PKH2, 100))
        self.assertEqual(L.parse_htlc(htlc_script(5))["lock_val"], 5)
        self.assertIsNone(L.parse_htlc(htlc_script(100)[:-1] + b"\xad"))                       # near miss
        self.assertEqual(L.parse_adaptor_lock(adaptor_lock_script()), (KAL, KAF))
        self.assertIsNone(L.parse_adaptor_lock(adaptor_lock_script()[:-1] + b"\xaf"))
        r = L.parse_adaptor_refund(adaptor_refund_script(10))
        self.assertEqual((r["pk1"], r["pk2"], r["pk3"], r["csv_val"]), (KAL, KAF, KAF if REAL else KAS, 10))

    def test_htlc_redeem_and_refund_segwit(self):
        sc = htlc_script(100)
        lock = ser_tx(2, [("aa" * 32, 0, b"", 0xFFFFFFFE)], [(150000, p2wsh(sc)), (5000, p2wpkh(PKH1))], 0, witness=[[b"s", b"k"]])
        lock_id = norm(lock)["txid"]
        redeem = ser_tx(2, [(lock_id, 0, b"", 0xFFFFFFFF)], [(149000, p2wpkh(PKH1))], 0, witness=[[b"\x30" + b"s" * 70, KAL, SECRET, b"\x01", sc]])
        refund = ser_tx(2, [(lock_id, 0, b"", 100)], [(149000, p2wpkh(PKH2))], 0, witness=[[b"\x30" + b"s" * 70, KAL, b"", sc]])
        _, spends, ev = self.run_block("btc", [cb_tx(), lock, redeem])
        self.assertEqual([e["kind"] for e in ev], ["htlc_redeem"])
        self.assertEqual(ev[0]["secret_hash"], SHASH.hex())
        self.assertEqual(ev[0]["secret"], SECRET.hex())
        self.assertEqual(ev[0]["amount_sat"], 150000)                                          # from the same-block index
        self.assertEqual(len(spends), 1)
        _, _, ev2 = self.run_block("btc", [cb_tx(), refund], prior_outputs={(lock_id, 0): {"value": 150000, "otype": "p2wsh", "program": "", "height": 90, "time": 1, "info": None}})
        self.assertEqual([e["kind"] for e in ev2], ["htlc_refund"])
        self.assertEqual(ev2[0]["amount_sat"], 150000)

    def test_htlc_p2sh_scriptsig_doge_style(self):
        sc = htlc_script(100, op_lock=0xB1)
        spend = ser_tx(1, [("bb" * 32, 1, push(b"\x30" + b"s" * 70) + push(KAL) + push(SECRET) + b"\x51" + push(sc), 0xFFFFFFFF)], [(1000, p2sh(b"x"))])
        _, _, ev = self.run_block("doge", [cb_tx(), spend])
        self.assertEqual([(e["kind"], e["extra"]["lock_op"]) for e in ev], [("htlc_redeem", "cltv")])

    def test_adaptor_paths(self):
        lock_sc, ref_sc = adaptor_lock_script(), adaptor_refund_script(10)
        lock = ser_tx(2, [("cc" * 32, 0, b"", 0xFFFFFFFE)], [(1_000_000, p2wsh(lock_sc)), (9_000, p2wpkh(PKH2))], 0, witness=[[b"s", b"k"]])
        lock_id = norm(lock)["txid"]
        wit = [b"", b"\x30" + b"a" * 70, b"\x30" + b"b" * 70, lock_sc]
        spend = ser_tx(2, [(lock_id, 0, b"", 0xFFFFFFFF)], [(999_000, p2wpkh(PKH1))], 0, witness=[wit])
        _, _, ev = self.run_block("btc", [cb_tx(), lock, spend])
        self.assertEqual([(e["kind"], e["confidence"]) for e in ev], [("adaptor_lock_spend", "likely")])
        spend_seq0 = ser_tx(2, [(lock_id, 0, b"", 0)], [(999_000, p2wpkh(PKH1))], 0, witness=[wit])      # the calibrated real-swap shape: nSequence 0
        _, _, ev_s = self.run_block("btc", [cb_tx(), lock, spend_seq0])
        self.assertEqual([(e["kind"], e["confidence"]) for e in ev_s], [("adaptor_lock_spend", "strong")])
        self.assertEqual(ev[0]["amount_sat"], 1_000_000)
        self.assertEqual(ev[0]["lock_tx"]["lt"], 0)
        # a plain 2-of-2 spend of a lock created by a wallet that sets nLockTime to the height is only a candidate
        lock2 = ser_tx(2, [("cd" * 32, 0, b"", 0xFFFFFFFD)], [(500_000, p2wsh(lock_sc))], 840000, witness=[[b"s", b"k"]])
        spend2 = ser_tx(2, [(norm(lock2)["txid"], 0, b"", 0xFFFFFFFF)], [(499_000, p2wpkh(PKH1))], 0, witness=[wit])
        _, _, ev2 = self.run_block("btc", [cb_tx(), lock2, spend2])
        self.assertEqual(ev2[0]["confidence"], "candidate")
        # a lock whose nLockTime sits just below its own height (block height 100 here) stays candidate even when spent with sequence 0: tested
        # 2026-11-20 and not adopted (no offer matches where about 3 were expected); same above its height or with a non-zero sequence
        lock3 = ser_tx(2, [("ce" * 32, 0, b"", 0xFFFFFFFE)], [(600_000, p2wsh(lock_sc))], 98, witness=[[b"s", b"k"]])
        spend3 = ser_tx(2, [(norm(lock3)["txid"], 0, b"", 0)], [(599_000, p2wpkh(PKH1))], 0, witness=[wit])
        _, _, ev5 = self.run_block("btc", [cb_tx(), lock3, spend3])
        self.assertEqual(ev5[0]["confidence"], "candidate")
        lock4 = ser_tx(2, [("cf" * 32, 0, b"", 0xFFFFFFFE)], [(700_000, p2wsh(lock_sc))], 840000, witness=[[b"s", b"k"]])
        spend4 = ser_tx(2, [(norm(lock4)["txid"], 0, b"", 0)], [(699_000, p2wpkh(PKH1))], 0, witness=[wit])
        _, _, ev6 = self.run_block("btc", [cb_tx(), lock4, spend4])
        self.assertEqual(ev6[0]["confidence"], "candidate")
        spend5 = ser_tx(2, [(norm(lock3)["txid"], 0, b"", 0xFFFFFFFF)], [(599_000, p2wpkh(PKH1))], 0, witness=[wit])
        _, _, ev7 = self.run_block("btc", [cb_tx(), lock3, spend5])
        self.assertEqual(ev7[0]["confidence"], "candidate")
        # refund path: lock-refund tx (CSV sequence, one output to a script hash), then the refund output is spent revealing the refund script
        refund_tx = ser_tx(2, [(lock_id, 0, b"", 10)], [(998_000, p2wsh(ref_sc))], 0, witness=[wit])
        _, _, ev3 = self.run_block("btc", [cb_tx(), lock, refund_tx])
        self.assertEqual([(e["kind"], e["confidence"]) for e in ev3], [("adaptor_refund_tx", "likely")])
        ref_id = norm(refund_tx)["txid"]
        ref_spend = ser_tx(2, [(ref_id, 0, b"", 10)], [(997_000, p2wpkh(PKH2))], 0, witness=[[b"", b"\x30" + b"c" * 70, ref_sc]])
        _, _, ev4 = self.run_block("btc", [cb_tx(), ref_spend], prior_outputs={(ref_id, 0): {"value": 998_000, "otype": "p2wsh", "program": "", "height": 95, "time": 1, "info": None}})
        self.assertEqual([(e["kind"], e["extra"]["branch"]) for e in ev4], [("adaptor_refund_spend", "timeout")])
        # database round trip: the refund spend upgrades the earlier refund tx to certain
        db = sqlite3.connect(":memory:")
        db.executescript(S.SCHEMA)
        hdr = {"time": 1, "prev": "p"}
        p2, sp, e3 = self.run_block("btc", [cb_tx(), lock, refund_tx])
        S.insert_block(db, "T", 100, "h" * 64, hdr, [], p2, sp, e3)
        S.insert_block(db, "T", 101, "i" * 64, hdr, [], [], [], ev4)
        row = db.execute("SELECT kind, confidence FROM swap_events WHERE txid=?", (ref_id,)).fetchone()
        self.assertEqual(row, ("adaptor_refund_tx", "certain"))
        # refund scripts from before BasicSwap 0.18 have no "1 CSV DROP" in the IF branch (missed until 2026-11-20); same path, tagged
        old_sc = ref_sc[:1] + ref_sc[4:]
        self.assertEqual(len(old_sc), len(ref_sc) - 3)
        o = L.parse_adaptor_refund(old_sc)
        self.assertEqual((o["pk1"], o["pk2"], o["csv_val"], o["old"]), (KAL, KAF, 10, True))
        self.assertFalse(L.parse_adaptor_refund(ref_sc)["old"])
        self.assertIsNone(L.parse_adaptor_refund(old_sc[:1] + b"\x53" + old_sc[2:]))                     # near miss: 3-of-n
        old_tx = ser_tx(2, [(lock_id, 0, b"", 10)], [(998_000, p2wsh(old_sc))], 0, witness=[wit])
        old_id = norm(old_tx)["txid"]
        old_spend = ser_tx(2, [(old_id, 0, b"", 10)], [(997_000, p2wpkh(PKH2))], 0, witness=[[b"", b"\x30" + b"c" * 70, old_sc]])
        _, _, ev8 = self.run_block("btc", [cb_tx(), old_spend], prior_outputs={(old_id, 0): {"value": 998_000, "otype": "p2wsh", "program": "", "height": 95, "time": 1, "info": None}})
        self.assertEqual([(e["kind"], e["extra"]["branch"], e["extra"].get("form")) for e in ev8], [("adaptor_refund_spend", "timeout", "pre_0_18")])
        p3, sp3, e9 = self.run_block("btc", [cb_tx(), lock, old_tx])
        S.insert_block(db, "T", 102, "j" * 64, hdr, [], p3, sp3, e9)
        S.insert_block(db, "T", 103, "k" * 64, hdr, [], [], [], ev8)
        self.assertEqual(db.execute("SELECT kind, confidence FROM swap_events WHERE txid=?", (old_id,)).fetchone(), ("adaptor_refund_tx", "certain"))

    def test_ordinary_traffic_is_ignored(self):
        multi3 = b"\x52\x21" + KAL + b"\x21" + KAF + b"\x21" + KAS + b"\x53\xae"              # 2-of-3: not a swap script
        t = ser_tx(2, [("dd" * 32, 0, b"", 0xFFFFFFFF)], [(1000, p2wpkh(PKH1))], 0, witness=[[b"", b"s1", b"s2", multi3]])
        pubkey_spend = ser_tx(1, [("ee" * 32, 0, push(b"\x30" + b"s" * 70) + push(KAL), 0xFFFFFFFF)], [(1, p2wpkh(PKH1))])
        _, _, ev = self.run_block("btc", [cb_tx(), t, pubkey_spend])
        self.assertEqual(ev, [])

    def test_bch_covenant_prefix(self):
        body = L.BCH_COVENANT_PREFIX + b"\x02\xe8\x03" + b"\x00" * 30 + b"\xba" + b"\x67" + b"\x01\x0a\xb2\x75" + b"\x00" * 30
        spend = ser_tx(2, [("ff" * 32, 0, push(b"\x01" * 10) + push(body), 0xFFFFFFFF)], [(5000, b"\xaa\x20" + b"\x01" * 32 + b"\x87")])
        _, _, ev = self.run_block("bch", [cb_tx(), spend])
        self.assertEqual([(e["kind"], e["confidence"]) for e in ev], [("bch_covenant_spend", "certain")])

    def test_dash_special_tx_and_doge_auxpow_parse(self):
        special = ser_tx(3 | (5 << 16), [("11" * 32, 0, b"", 0xFFFFFFFF)], [(1, b"\x51")], 0, extra=b"\x01\x02\x03\x04\x05")
        plain = ser_tx(1, [("22" * 32, 0, b"", 0xFFFFFFFF)], [(2, b"\x51")])
        hdr, txs = L.parse_raw_block(block([cb_tx(), special, plain]), "dash")
        self.assertEqual(len(txs), 3)
        self.assertEqual(txs[2]["vout"][0]["value"], 2)
        aux = cb_tx() + b"\x00" * 32 + varint(2) + b"\x01" * 64 + struct.pack("<I", 0) + varint(1) + b"\x02" * 32 + struct.pack("<I", 0) + b"\x00" * 80
        hdr, txs = L.parse_raw_block(block([cb_tx(), plain], version=0x00620104, auxpow=aux), "doge")
        self.assertEqual(len(txs), 2)
        self.assertEqual(txs[1]["txid"], norm(plain)["txid"])

    def test_segwit_txid_excludes_witness(self):
        a = ser_tx(2, [("12" * 32, 0, b"", 0xFFFFFFFF)], [(1, b"\x51")], 0, witness=[[b"one"]])
        b = ser_tx(2, [("12" * 32, 0, b"", 0xFFFFFFFF)], [(1, b"\x51")], 0, witness=[[b"other", b"items"]])
        self.assertEqual(norm(a)["txid"], norm(b)["txid"])
        self.assertEqual(norm(a)["vin"][0]["witness"], [b"one"])

    def test_json_adapter_values_and_prevouts(self):
        from decimal import Decimal
        blk = {"time": 5, "previousblockhash": "p", "version": 1, "tx": [
            {"txid": "aa" * 32, "version": 2, "locktime": 0, "vin": [{"coinbase": "0102", "sequence": 4294967295}], "vout": [{"value": Decimal("12.5"), "scriptPubKey": {"hex": "51"}}]},
            {"txid": "bb" * 32, "version": 2, "locktime": 0, "vin": [{"txid": "cc" * 32, "vout": 1, "scriptSig": {"hex": ""}, "txinwitness": ["00", "ab"], "sequence": 1,
                                                                        "prevout": {"value": Decimal("0.00012345"), "scriptPubKey": {"hex": "0014" + "11" * 20}}}],
             "vout": [{"value": Decimal("0.0001"), "scriptPubKey": {"hex": "0014" + "22" * 20}}]}]}
        hdr, txs = L.norm_json_block(blk, "btc")
        self.assertEqual(txs[0]["vout"][0]["value"], 1_250_000_000)
        self.assertTrue(txs[0]["coinbase"])
        self.assertEqual(txs[1]["vin"][0]["prevout"]["value"], 12345)
        self.assertEqual(txs[1]["vin"][0]["witness"], [b"\x00", b"\xab"])


class OlderRange(unittest.TestCase):
    """the older-range backfill: scanned ascending, resumable after a stop, no gaps, forward scan unaffected"""

    class FakeSource:
        lat = []

        def __init__(self, tip, first):
            self._tip, self._first = tip, first

        def tip(self):
            return self._tip

        def tip_time(self):
            import time
            return int(time.time())

        def earliest(self, want, tip):
            return max(want, self._first)

        def blockhash(self, h):
            return "%064x" % h

        def block(self, h, bh):
            return {"time": 1_700_000_000 + h, "prev": "%064x" % (h - 1)}, []

    def test_older_range_after_the_window_grows(self):
        import logging
        import types
        S.log = logging.getLogger("t")
        cfg = S.COINS["BTC"]
        saved_rps, saved_make = cfg.get("rps"), S.make_source
        cfg["rps"] = 100000
        db = sqlite3.connect(":memory:")
        db.executescript(S.SCHEMA)
        src = self.FakeSource(1000, 900)                      # window 900-1000, 144 blocks a day
        S.make_source = lambda coin, c: src
        a = types.SimpleNamespace(rps=100000, backfill_days=0.7, max_blocks=0, dry_run=False, no_wide=True, no_extend_back=False)
        try:
            self.assertEqual(S.scan_coin(db, "BTC", a, 60), "caught_up")
            lo, hi = db.execute("SELECT MIN(height), MAX(height) FROM blocks").fetchone()
            self.assertEqual((lo, hi), (1000 - int(144 * 0.7), 1000))     # 900..1000 start = 899.2 -> 900 (earliest clamps)
            # the window grows downwards (header extension): an older range appears
            src._first = 700
            a.backfill_days, a.max_blocks = 1.5, 40
            self.assertEqual(S.scan_coin(db, "BTC", a, 60), "partial")     # stopped after 40 blocks
            mn = db.execute("SELECT MIN(height) FROM blocks").fetchone()[0]
            self.assertEqual(mn, 1000 - 216)                               # older range starts at the new lower edge
            self.assertEqual(db.execute("SELECT COUNT(*) FROM blocks WHERE height BETWEEN ? AND 899", (mn,)).fetchone()[0], 40)
            a.max_blocks = 0
            self.assertEqual(S.scan_coin(db, "BTC", a, 60), "caught_up")   # resumes where it stopped
            n = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
            self.assertEqual(n, 1000 - (1000 - 216) + 1)                   # contiguous: every height from the new edge to the tip
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backfill").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0], 0)
        finally:
            cfg["rps"], S.make_source = saved_rps, saved_make

    def test_tip_first_and_index_protection(self):
        """the forward range (new blocks) is scanned before the older range, and pruning keeps the window behind both resume points"""
        import logging
        import types
        S.log = logging.getLogger("t")
        cfg = S.COINS["BTC"]
        saved_rps, saved_make = cfg.get("rps"), S.make_source
        cfg["rps"] = 100000
        db = sqlite3.connect(":memory:")
        db.executescript(S.SCHEMA)
        src = self.FakeSource(1000, 900)
        S.make_source = lambda coin, c: src
        a = types.SimpleNamespace(rps=100000, backfill_days=0.7, max_blocks=0, dry_run=False, no_wide=True, no_extend_back=False)
        try:
            S.scan_coin(db, "BTC", a, 60)
            src._tip, src._first = 1050, 700                       # 50 new blocks and a deeper window at the same time
            a.backfill_days, a.max_blocks = 1.5, 30
            S.scan_coin(db, "BTC", a, 60)
            self.assertEqual(db.execute("SELECT MAX(height) FROM blocks").fetchone()[0], 1030)      # the 30 blocks went to the tip side
            self.assertEqual(db.execute("SELECT MIN(height) FROM blocks").fetchone()[0], 900)
            a.max_blocks = 0
            S.scan_coin(db, "BTC", a, 60)
            self.assertEqual(db.execute("SELECT MAX(height) FROM blocks").fetchone()[0], 1050)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backfill").fetchone()[0], 0)
            self.assertEqual(S.coverage(db, "BTC")["missing_inside_range"], 0)
        finally:
            cfg["rps"], S.make_source = saved_rps, saved_make
        # pruning: rows far behind the tip are removed, but not the window behind the older-range frontier or the forward frontier
        db2 = sqlite3.connect(":memory:")
        db2.executescript(S.SCHEMA)
        keep = int(cfg["bpd"] * S.P2SH_RETAIN_DAYS["BTC"])                                           # 1008 blocks
        for h in (10, 5000, 5900, 9000, 9990, 10000):
            db2.execute("INSERT INTO p2sh_outputs VALUES ('BTC',?,0,1,'p2sh','00',?,0,'{}')", ("t%d" % h, h))
        db2.execute("INSERT INTO blocks VALUES ('BTC',10000,'h',0,0,'p')")
        db2.execute("INSERT INTO backfill VALUES ('BTC',5500,9000)")                                  # older range resumes at 5500: rows 5500-keep..5500 must survive
        S.finalize_run(db2, "BTC", cfg, 20000)
        left = sorted(r[0] for r in db2.execute("SELECT height FROM p2sh_outputs"))
        self.assertEqual(left, [5000, 9000, 9990, 10000])                                              # 5000 inside (5500-1008, 5500); 9000, 9990, 10000 inside the forward window (10000-1008, 10000); 10 and 5900 pruned


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Handler.seen.append((self.path, json.loads(body)["method"]))
        out = json.dumps({"result": 123, "error": None, "id": "x"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class Allowlist(unittest.TestCase):
    def test_wallet_and_other_methods_are_refused_before_any_request(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        rpc = S.ChainRPC("DASH", "127.0.0.1", srv.server_address[1], "u", "p")
        for m, p in (("getnewaddress", []), ("sendtoaddress", ["x", 1]), ("getbalance", []), ("dumpprivkey", ["x"]), ("walletpassphrase", ["x", 1]), ("getrawtransaction", ["a" * 64]),
                     ("getblock", ["zz", 0]), ("getblock", ["a" * 64, 5]), ("getblock", ["a" * 64]), ("getblockhash", ["1"]), ("getblockcount", [1]), ("listunspent", []),
                     ("gettxoutsetinfo", []), ("help", []), ("stop", [])):
            with self.assertRaises(S.RPCNotAllowed, msg=m):
                rpc.call(m, p)
        self.assertEqual(Handler.seen, [])
        self.assertEqual(rpc.call("getblockcount"), 123)
        rpc.call("getblock", ["a" * 64, 0])
        rpc.call("getblock", ["a" * 64, True, True])
        rpc.call("getblockhash", [5])
        self.assertTrue(all(path == "/" for path, _ in Handler.seen))                             # never /wallet/...
        self.assertEqual([m for _, m in Handler.seen], ["getblockcount", "getblock", "getblock", "getblockhash"])
        srv.shutdown()
        srv.server_close()

    def test_port_and_host_guards(self):
        import tempfile
        old = S.BASICSWAP_JSON
        d = tempfile.mkdtemp()
        p = os.path.join(d, "bs.json")
        try:
            S.BASICSWAP_JSON = p
            for host, port, ok in (("127.0.0.1", 9998, True), ("127.0.0.1", 18082, False), ("127.0.0.1", 9999, False), ("10.0.0.5", 9998, False)):   # export-ok: deliberate non-loopback host (must be refused)
                with open(p, "w") as fh:
                    json.dump({"chainclients": {"dash": {"rpchost": host, "rpcport": port, "rpcuser": "u", "rpcpassword": "p"}}}, fh)
                if ok:
                    self.assertEqual(S.load_credentials("DASH")[1], 9998)
                else:
                    with self.assertRaises(S.RPCNotAllowed):
                        S.load_credentials("DASH")
        finally:
            S.BASICSWAP_JSON = old
        for c in S.COINS.values():
            self.assertNotIn(c.get("port"), S.PROTECTED_PORTS)
        self.assertEqual({k for k, c in S.COINS.items() if c["mode"] == "p2p"}, {"BTC", "LTC"})     # no local chain and no sync for these two


if __name__ == "__main__":
    print("using the real BasicSwap builders:", REAL)
    unittest.main(verbosity=1)
