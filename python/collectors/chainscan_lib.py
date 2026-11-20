"""Pure parsing and recognition code for the on-chain BasicSwap scan (no network, no database, stdlib only).

Plan and rules: docs/method.md.  The templates below are the ones built by BasicSwap (checked against the builders in
basicswap/basicswap/protocols/atomic_swap_1.py and interface/btc/btc.py, interface/bch/bch.py - see test_chainscan.py, which rebuilds them
with the real builders when the node venv is available).

Normalised transaction (every block source is converted to this):
  {"txid": hex, "version": int, "locktime": int, "coinbase": bool,
   "vin":  [{"txid": hex|None, "vout": int, "script_sig": bytes, "sequence": int, "witness": [bytes], "prevout": None | {"value": sat, "script": bytes}}],
   "vout": [{"value": sat, "script": bytes}]}
"""
import hashlib
import json
import struct
from decimal import Decimal

SAT = 100000000

# ---------------------------------------------------------------- hashing / reading helpers


def sha256(b):
    return hashlib.sha256(b).digest()


def dsha256(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


class Reader:
    def __init__(self, buf, pos=0):
        self.buf, self.pos = buf, pos

    def read(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise ValueError("read past end")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self):
        return self.read(1)[0]

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def i32(self):
        return struct.unpack("<i", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def varint(self):
        n = self.u8()
        if n < 0xFD:
            return n
        if n == 0xFD:
            return struct.unpack("<H", self.read(2))[0]
        if n == 0xFE:
            return self.u32()
        return self.u64()

    def varbytes(self):
        return self.read(self.varint())


# ---------------------------------------------------------------- raw block / transaction parsing (Bitcoin family)


def parse_tx(r, family="btc"):
    """Parse one transaction from a Reader.  family: btc (segwit aware), ltc (segwit; MWEB not supported here), bch, doge, dash (special payloads)."""
    if family == "part" and r.buf[r.pos] == PART_TX_VERSION:
        return parse_part_tx(r)
    start = r.pos
    version = r.i32()
    segwit = False
    if family in ("btc", "ltc") and r.pos + 1 < len(r.buf) and r.buf[r.pos] == 0x00 and r.buf[r.pos + 1] != 0x00:
        flag = r.buf[r.pos + 1]
        if flag & 1:
            segwit = True
        r.pos += 2
    body_start = r.pos
    n_in = r.varint()
    vin = []
    for _ in range(n_in):
        prev = r.read(32)[::-1].hex()
        vout = r.u32()
        script_sig = r.varbytes()
        seq = r.u32()
        coinbase = (prev == "00" * 32 and vout == 0xFFFFFFFF)
        vin.append({"txid": None if coinbase else prev, "vout": vout, "script_sig": script_sig, "sequence": seq, "witness": [], "prevout": None, "coinbase": coinbase})
    n_out = r.varint()
    vout_l = []
    for _ in range(n_out):
        value = r.u64()
        script = r.varbytes()
        vout_l.append({"value": value, "script": script})
    body_end = r.pos
    if segwit:
        for i in range(n_in):
            vin[i]["witness"] = [r.varbytes() for _ in range(r.varint())]
    locktime = r.u32()
    if family == "dash" and (version & 0xFFFF) >= 3 and ((version >> 16) & 0xFFFF) != 0:
        r.varbytes()                                           # DIP2/DIP3 extra payload
    end = r.pos
    raw = r.buf[start:end]
    if segwit:
        stripped = raw[:4] + r.buf[body_start:body_end] + raw[-4:]
        txid = dsha256(stripped)[::-1].hex()
    else:
        txid = dsha256(raw)[::-1].hex()
    return {"txid": txid, "version": version, "locktime": locktime, "coinbase": bool(vin and vin[0]["coinbase"]), "vin": vin, "vout": vout_l}


PART_TX_VERSION = 0xA0
PART_OUT_STANDARD, PART_OUT_CT, PART_OUT_RINGCT, PART_OUT_DATA = 1, 2, 3, 4
PART_ANON_MARKER = 0xFFFFFFA0


def parse_part_tx(r):
    """Particl transaction (version byte 0xA0; layout from BasicSwap's test_framework/messages.py CTransaction / CTxOutPart, 2026-11-19):
    version (1 byte 0xA0 + 1 byte type), nLockTime (4), inputs (as Bitcoin; an anon input, prevout index 0xFFFFFFA0, adds a scriptData stack after
    nSequence), outputs each prefixed by a type byte:
      1 standard: value (8) + script;  2 CT: commitment (33) + data + script + rangeproof;  3 RingCT: pubkey (33) + commitment (33) + data + rangeproof;
      4 data: data.
    then one witness stack per input (always present in blocks).  txid = sha256d of the serialisation without witness, with every rangeproof
    replaced by an empty vector (CTxOutPart.serialize(with_witness=False)).  Hidden-value outputs (CT, RingCT) get value None."""
    start = r.pos
    version = r.buf[r.pos] | (r.buf[r.pos + 1] << 8)
    r.pos += 2
    locktime = r.u32()
    n_in = r.varint()
    vin = []
    for _ in range(n_in):
        prev = r.read(32)[::-1].hex()
        vout = r.u32()
        script_sig = r.varbytes()
        seq = r.u32()
        if vout == PART_ANON_MARKER:                             # anon (RingCT) input: scriptData stack follows; part of the txid serialisation
            for _ in range(r.varint()):
                r.varbytes()
        vin.append({"txid": prev, "vout": vout, "script_sig": script_sig, "sequence": seq, "witness": [], "prevout": None, "coinbase": False})
    stripped = [r.buf[start:r.pos]]
    n_out = r.varint()
    stripped.append(_compact(n_out))
    vout_l = []
    for _ in range(n_out):
        o0 = r.pos
        typ = r.buf[r.pos]
        r.pos += 1
        if typ == PART_OUT_STANDARD:
            value = r.u64()
            script = r.varbytes()
            stripped.append(r.buf[o0:r.pos])
        elif typ == PART_OUT_CT:
            r.read(33); r.varbytes(); script = r.varbytes()
            keep = r.pos
            r.varbytes()                                         # rangeproof (stubbed in the txid serialisation)
            stripped.append(r.buf[o0:keep] + b"\x00")
            value = None
        elif typ == PART_OUT_RINGCT:
            r.read(33); r.read(33); r.varbytes()
            keep = r.pos
            r.varbytes()
            stripped.append(r.buf[o0:keep] + b"\x00")
            value, script = None, b""
        elif typ == PART_OUT_DATA:
            r.varbytes()
            stripped.append(r.buf[o0:r.pos])
            value, script = None, b""
        else:
            raise ValueError("unknown Particl output type %d" % typ)
        vout_l.append({"value": value, "script": script, "ptype": typ})
    for i in range(n_in):
        vin[i]["witness"] = [r.varbytes() for _ in range(r.varint())]
    txid = dsha256(b"".join(stripped))[::-1].hex()
    coinbase = bool(vin) and vin[0]["txid"] == "00" * 32 and vin[0]["vout"] == 0xFFFFFFFF
    if coinbase:
        vin[0].update(txid=None, coinbase=True)
    return {"txid": txid, "version": version, "locktime": locktime, "coinbase": coinbase, "vin": vin, "vout": vout_l}


def _compact(n):
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def parse_raw_block(raw, family="btc"):
    """-> ({"time": int, "prev": hex, "version": int}, [tx, ...]).  The block hash is NOT computed (Dogecoin and Dash use other hash functions): the caller knows it."""
    r = Reader(raw)
    if family == "part":                                      # Particl: 112-byte header (adds the witness merkle root); txs; then the block signature (ignored)
        hdr = r.read(112)
        n_tx = r.varint()
        txs = [parse_part_tx(r) if r.buf[r.pos] == PART_TX_VERSION else parse_tx(r, "btc") for _ in range(n_tx)]
        return {"time": struct.unpack("<I", hdr[100:104])[0], "prev": hdr[4:36][::-1].hex(), "version": struct.unpack("<i", hdr[0:4])[0]}, txs
    hdr = r.read(80)
    version = struct.unpack("<i", hdr[0:4])[0]
    prev = hdr[4:36][::-1].hex()
    time = struct.unpack("<I", hdr[68:72])[0]
    if family == "doge" and (version & 0x100):
        parse_tx(r, "bch")                                    # parent coinbase transaction (plain serialisation)
        r.read(32)                                            # parent block hash
        r.read(32 * r.varint()); r.read(4)                    # coinbase merkle branch, index
        r.read(32 * r.varint()); r.read(4)                    # chain merkle branch, index
        r.read(80)                                            # parent header
    n_tx = r.varint()
    txs = [parse_tx(r, family) for _ in range(n_tx)]
    return {"time": time, "prev": prev, "version": version}, txs


# ---------------------------------------------------------------- JSON block adapters (BTC verbosity 3, LTC verbosity 2, DCR decoded)


def _sat(v):
    if isinstance(v, Decimal):
        return int((v * SAT).to_integral_value())
    return int(round(float(v) * SAT))


def norm_json_tx(t, family="btc"):
    vin = []
    for i in t.get("vin", []):
        if "coinbase" in i:
            vin.append({"txid": None, "vout": 0xFFFFFFFF, "script_sig": bytes.fromhex(i["coinbase"]), "sequence": i.get("sequence", 0xFFFFFFFF),
                        "witness": [bytes.fromhex(w) for w in i.get("txinwitness", [])], "prevout": None, "coinbase": True})
            continue
        po = i.get("prevout")
        prevout = None
        if po and "scriptPubKey" in po:
            prevout = {"value": _sat(po["value"]), "script": bytes.fromhex(po["scriptPubKey"]["hex"])}
        elif family == "dcr" and "amountin" in i:
            prevout = {"value": _sat(i["amountin"]), "script": None}
        vin.append({"txid": i["txid"], "vout": i["vout"], "script_sig": bytes.fromhex((i.get("scriptSig") or {}).get("hex", "")), "sequence": i.get("sequence", 0xFFFFFFFF),
                    "witness": [bytes.fromhex(w) for w in i.get("txinwitness", [])], "prevout": prevout, "coinbase": False})
    vout = [{"value": _sat(o["value"]), "script": bytes.fromhex(o["scriptPubKey"]["hex"])} for o in t.get("vout", [])]
    return {"txid": t["txid"], "version": t.get("version", 0), "locktime": t.get("locktime", 0), "coinbase": bool(vin and vin[0]["coinbase"]), "vin": vin, "vout": vout}


def norm_json_block(blk, family="btc"):
    """blk: decoded getblock result.  Bitcoin-like: blk['tx'] is a list of tx dicts.  DCR: blk['rawtx'] (regular tree only)."""
    txs_json = blk.get("rawtx") if family == "dcr" else blk.get("tx", [])
    txs = [norm_json_tx(t, family) for t in txs_json if isinstance(t, dict)]
    return {"time": blk.get("time", 0), "prev": blk.get("previousblockhash", ""), "version": blk.get("version", 0)}, txs


# ---------------------------------------------------------------- script helpers


def script_num(data):
    if not data:
        return 0
    n = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        n &= ~(0x80 << (8 * (len(data) - 1)))
        return -n
    return n


def parse_pushes(script):
    """list of pushed byte strings, or None if the script holds anything but pushes."""
    out, i, n = [], 0, len(script)
    while i < n:
        op = script[i]
        i += 1
        if op == 0x00:
            out.append(b"")
        elif op <= 0x4B:
            out.append(script[i:i + op]); i += op
        elif op == 0x4C:
            if i >= n:
                return None
            ln = script[i]; i += 1
            out.append(script[i:i + ln]); i += ln
        elif op == 0x4D:
            if i + 2 > n:
                return None
            ln = struct.unpack("<H", script[i:i + 2])[0]; i += 2
            out.append(script[i:i + ln]); i += ln
        elif op == 0x4F:
            out.append(b"\x81")
        elif 0x51 <= op <= 0x60:
            out.append(bytes([op - 0x50]))
        else:
            return None
        if i > n:
            return None
    return out


def out_type(script):
    """(type, program) for the standard output scripts we care about."""
    n = len(script)
    if n == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
        return "p2pkh", script[3:23]
    if n == 23 and script[:2] == b"\xa9\x14" and script[22] == 0x87:
        return "p2sh", script[2:22]
    if n == 22 and script[:2] == b"\x00\x14":
        return "p2wpkh", script[2:]
    if n == 34 and script[:2] == b"\x00\x20":
        return "p2wsh", script[2:]
    if n == 35 and script[:2] == b"\xaa\x20" and script[34] == 0x87:
        return "p2sh32", script[2:34]
    if n == 34 and script[:2] == b"\x51\x20":
        return "p2tr", script[2:]
    if n >= 1 and script[0] == 0x6A:
        return "opreturn", b""
    return "other", b""


SCRIPTHASH_TYPES = ("p2sh", "p2wsh", "p2sh32")


def input_script(vin):
    """The script a spend reveals (witness script for P2WSH and nested P2WSH, last push of the scriptSig otherwise), plus the stack items before it."""
    w = vin.get("witness") or []
    if len(w) >= 2:
        return w[-1], list(w[:-1])
    pushes = parse_pushes(vin.get("script_sig") or b"")
    if pushes and len(pushes) >= 1:
        last = pushes[-1]
        if len(last) in (33, 65) and last[:1] in (b"\x02", b"\x03", b"\x04"):
            return None, []                                        # P2PKH-style: last push is a public key
        if len(last) in range(70, 74) and last[:1] == b"\x30":
            return None, []                                        # bare signature
        return last, pushes[:-1]
    return None, []


# ---------------------------------------------------------------- swap templates


OP_IF, OP_ELSE, OP_ENDIF, OP_DROP, OP_DUP = 0x63, 0x67, 0x68, 0x75, 0x76
OP_CSV, OP_CLTV = 0xB2, 0xB1


def _num_push(s, o):
    """decode the script-number push at s[o]; -> (value, next_offset) or None"""
    if o >= len(s):
        return None
    b = s[o]
    if b == 0x00:
        return 0, o + 1
    if 0x51 <= b <= 0x60:
        return b - 0x50, o + 1
    if 1 <= b <= 5 and o + 1 + b <= len(s):
        return script_num(s[o + 1:o + 1 + b]), o + 1 + b
    return None


def parse_htlc(s):
    """BasicSwap secret-hash contract (protocols/atomic_swap_1.buildContractScript).  -> dict or None"""
    if len(s) < 90 or len(s) > 100:
        return None
    if not (s[0] == OP_IF and s[1] == 0x82 and s[2] == 0x01 and s[3] == 0x20 and s[4] == 0x88 and s[5] in (0xA8, 0xC0) and s[6] == 0x20):
        return None
    secret_hash = s[7:39]
    if not (s[39] == 0x88 and s[40] == OP_DUP and s[41] == 0xA9 and s[42] == 0x14 and s[63] == OP_ELSE):
        return None
    pkh_redeem = s[43:63]
    r = _num_push(s, 64)
    if r is None:
        return None
    lock_val, o = r
    if o + 28 != len(s) or s[o] not in (OP_CSV, OP_CLTV) or s[o + 1] != OP_DROP or s[o + 2] != OP_DUP or s[o + 3] != 0xA9 or s[o + 4] != 0x14:
        return None
    pkh_refund = s[o + 5:o + 25]
    if s[o + 25:o + 28] != bytes([OP_ENDIF, 0x88, 0xAC]):
        return None
    return {"secret_hash": secret_hash, "pkh_redeem": pkh_redeem, "pkh_refund": pkh_refund, "lock_val": lock_val,
            "lock_op": "csv" if s[o] == OP_CSV else "cltv", "hash_op": "sha256" if s[5] == 0xA8 else "sha256_decred"}


def parse_adaptor_lock(s):
    """71-byte 2-of-2 (interface/btc extractScriptLockScriptValues) -> (pk1, pk2) or None"""
    if len(s) == 71 and s[0] == 0x52 and s[1] == 0x21 and s[35] == 0x21 and s[69] == 0x52 and s[70] == 0xAE:
        return s[2:35], s[36:69]
    return None


def parse_adaptor_refund(s):
    """lock refund output script (genScriptLockRefundTxScript) -> dict or None.
    BasicSwap 0.18+ (2026-08-24): IF 1 CSV DROP 2 <pk> <pk> 2 CHECKMULTISIG ELSE <n> CSV DROP <pk> CHECKSIG ENDIF.
    Before 0.18 the IF branch had no "1 CSV DROP" (115-byte scripts); those refunds were missed until 2026-11-20 ("form": "pre_0_18")."""
    if 114 <= len(s) <= 125 and s[0] == OP_IF and s[1] == 0x51 and s[2] == OP_CSV and s[3] == OP_DROP:
        b, old = 4, False
    elif 111 <= len(s) <= 122 and s[0] == OP_IF:
        b, old = 1, True
    else:
        return None
    if not (s[b] == 0x52 and s[b + 1] == 0x21 and s[b + 35] == 0x21 and s[b + 69] == 0x52 and s[b + 70] == 0xAE and s[b + 71] == OP_ELSE):
        return None
    r = _num_push(s, b + 72)
    if r is None:
        return None
    csv_val, o = r
    if o + 38 != len(s) or s[o] != OP_CSV or s[o + 1] != OP_DROP or s[o + 2] != 0x21 or s[o + 36] != 0xAC or s[o + 37] != OP_ENDIF:
        return None
    return {"pk1": s[b + 2:b + 35], "pk2": s[b + 36:b + 69], "pk3": s[o + 3:o + 36], "csv_val": csv_val, "old": old}


BCH_COVENANT_PREFIX = bytes([0xC3, 0x51, 0x9D, 0xC4, 0x51, 0x9D])   # OP_TXINPUTCOUNT 1 NUMEQUALVERIFY OP_TXOUTPUTCOUNT 1 NUMEQUALVERIFY


def parse_bch_covenant(s):
    if len(s) > 60 and s.startswith(BCH_COVENANT_PREFIX) and b"\xba" in s and bytes([OP_CSV, OP_DROP]) in s:
        return {"len": len(s)}
    return None


# ---------------------------------------------------------------- block scanning


def txinfo(tx):
    """compact, JSON-able summary of the transaction that created a script-hash output (kept in the rolling index)"""
    ins = []
    for v in tx["vin"][:4]:
        po = v.get("prevout") or {}
        ins.append([v["txid"], v["vout"], po.get("value"), (po.get("script") or b"").hex() or None])
    outs = []
    for o in tx["vout"][:4]:
        t, p = out_type(o["script"])
        outs.append([t, p.hex(), o["value"]])
    return {"v": tx["version"], "lt": tx["locktime"], "nin": len(tx["vin"]), "nout": len(tx["vout"]), "ins": ins, "outs": outs}   # first four inputs and outputs only (disk)


def swap_tx_version(family):
    """The transaction version BasicSwap writes for its swap transactions on this chain (interface txVersion()): 2 on the Bitcoin family,
    0xA0 on Particl (part.py; fixed 2026-11-19: with 2 the strong tier never matched a PART lock spend, 222 real ones were left as candidates)."""
    return 0xA0 if family == "part" else 2


def blind_outputs(tx, family):
    """Particl BLIND (CT) swap transaction shape (part.py PARTInterfaceBlind createSCLockSpendTx / createSCLockRefundTx, 2026-11-20): the daemon
    writes one data output (the fee) and one or two blinded outputs (the destination and a zero-value change to a junk key), values hidden.
    Returns the script types of the blinded outputs, or None when the transaction does not have that shape."""
    if family != "part":
        return None
    data = sum(1 for o in tx["vout"] if o.get("ptype") == PART_OUT_DATA)
    ct = [o for o in tx["vout"] if o.get("ptype") == PART_OUT_CT]
    if data != 1 or not (1 <= len(ct) <= 2) or data + len(ct) != len(tx["vout"]):
        return None
    return [out_type(o["script"])[0] for o in ct]


def scan_block(coin, family, height, block_hash, header, txs, lookup_output, wide=True, wide_max_hex=300, index_only=False):
    """Recognise swap spends in one block.
    lookup_output(prev_txid, prev_vout) -> dict(value, otype, program, height, time, info) or None  (the rolling script-hash output index; the
    caller must have inserted this block's own script-hash outputs first so same-block spends resolve).
    Returns (p2sh_rows, spends_rows, events).  Pure function: no I/O."""
    t_block = header["time"]
    p2sh_rows, spends, events = [], [], []
    for tx in txs:
        info_cache = None
        for n, o in enumerate(tx["vout"]):
            t, prog = out_type(o["script"])
            if t in SCRIPTHASH_TYPES:
                if info_cache is None:
                    info_cache = json.dumps(txinfo(tx), separators=(",", ":"))
                p2sh_rows.append((coin, tx["txid"], n, o["value"], t, prog.hex(), height, t_block, info_cache))
    if index_only:
        return p2sh_rows, spends, events
    for tx in txs:
        if tx["coinbase"]:
            continue
        nin, nout = len(tx["vin"]), len(tx["vout"])
        for i, v in enumerate(tx["vin"]):
            script, stack = input_script(v)
            if script is None or not (34 <= len(script) <= 1000):
                continue
            prev = lookup_output(v["txid"], v["vout"])
            amount = (v.get("prevout") or {}).get("value")
            if amount is None and prev:
                amount = prev["value"]
            sh = sha256(script).hex()
            if wide:
                spends.append((coin, height, t_block, tx["txid"], i, sh, len(script), script.hex() if len(script) <= wide_max_hex else None,
                               len(v.get("witness") or []), nin, nout, v["txid"], v["vout"], amount))
            ev = None
            h = parse_htlc(script)
            if h:
                secret = next((x for x in stack if len(x) == 32 and sha256(x) == h["secret_hash"]), None)
                if secret is not None:
                    kind, conf = "htlc_redeem", "certain"
                elif stack and stack[-1] == b"":
                    kind, conf = "htlc_refund", "certain"
                else:
                    kind, conf = "htlc_spend_unknown", "certain"
                ev = dict(kind=kind, protocol="secret_hash", confidence=conf, secret_hash=h["secret_hash"].hex(), secret=secret.hex() if secret else None,
                          lock_val=h["lock_val"], extra={"lock_op": h["lock_op"], "hash_op": h["hash_op"], "pkh_redeem": h["pkh_redeem"].hex(), "pkh_refund": h["pkh_refund"].hex()})
            else:
                a = parse_adaptor_refund(script)
                if a:
                    branch = "lock_refund_complete" if (len(stack) >= 3 and stack[0] == b"") else "timeout"
                    ev = dict(kind="adaptor_refund_spend", protocol="xmr_adaptor", confidence="certain", lock_val=a["csv_val"],
                              extra={"branch": branch, "pk1": a["pk1"].hex(), "pk2": a["pk2"].hex(), "pk3": a["pk3"].hex()})
                    if a["old"]:
                        ev["extra"]["form"] = "pre_0_18"
                else:
                    k = parse_adaptor_lock(script)
                    if k:
                        outs = [out_type(o["script"])[0] for o in tx["vout"]]
                        csv_seq = (v["sequence"] & 0x80000000) == 0 and v["sequence"] != 0
                        blind = blind_outputs(tx, family)                  # Particl blind swap (2026-11-20): data + blinded outputs, values hidden
                        blind_refund = (blind is not None and nin == 1 and csv_seq and sum(t in ("p2wsh", "p2sh") for t in blind) == 1
                                        and all(t in ("p2wsh", "p2sh", "p2pkh", "p2wpkh") for t in blind))
                        if (nin == 1 and nout == 1 and outs[0] in ("p2wsh", "p2sh") and csv_seq) or blind_refund:
                            ev = dict(kind="adaptor_refund_tx", protocol="xmr_adaptor", confidence="likely", lock_val=v["sequence"] & 0xFFFF,
                                      extra={"pk1": k[0].hex(), "pk2": k[1].hex()})
                            if blind_refund:
                                ev["extra"]["blind"] = True
                        else:
                            lock_lt = None
                            if prev and prev.get("info"):
                                lock_lt = prev["info"].get("lt")
                            plain = nin == 1 and nout == 1 and outs[0] in ("p2pkh", "p2wpkh") and tx["version"] == swap_tx_version(family)
                            blind_plain = (blind is not None and nin == 1 and all(t in ("p2pkh", "p2wpkh") for t in blind)
                                           and tx["version"] == swap_tx_version(family))
                            plain = plain or blind_plain
                            seq0 = v["sequence"] == 0                      # calibrated on our own swap (2026-07-06): BasicSwap leaves nSequence at 0 on the lock spend; ordinary wallets and Lightning use 0xffffffff
                            # strong = every calibrated feature of the real swap present; likely = plain spend with an nLockTime-0 lock; candidate = bare 2-of-2 spend
                            # strong needs lock nLockTime 0. Tested 2026-11-20 and NOT adopted: also accepting a lock nLockTime 0-100 blocks below the
                            # lock height would add 46 spends, but none of the 35 since board data began matched an offer (3.3 expected at the
                            # strong tier's rate, chance 1.6 %); see docs/method.md
                            conf = "strong" if (plain and seq0 and lock_lt in (None, 0)) else ("likely" if plain and lock_lt in (None, 0) else "candidate")
                            ev = dict(kind="adaptor_lock_spend", protocol="xmr_adaptor", confidence=conf,
                                      extra={"pk1": k[0].hex(), "pk2": k[1].hex(), "plain_spend": plain, "seq0": seq0, "lock_nlocktime": lock_lt,
                                             "lock_age_blocks": (height - prev["height"]) if prev else None})
                            if blind_plain:
                                ev["extra"]["blind"] = True
                    elif family == "bch":
                        c = parse_bch_covenant(script)
                        if c:
                            ev = dict(kind="bch_covenant_spend", protocol="xmr_bch_covenant", confidence="certain", extra=c)
            if ev:
                ev.update(coin=coin, height=height, block_hash=block_hash, block_time=t_block, txid=tx["txid"], vin=i, script_sha256=sh,
                          amount_sat=amount, prev_txid=v["txid"], prev_vout=v["vout"], sequence=v["sequence"], tx_nin=nin, tx_nout=nout,
                          tx_version=tx["version"], tx_locktime=tx["locktime"],
                          outputs=[[*out_type(o["script"])[:1], out_type(o["script"])[1].hex(), o["value"]] for o in tx["vout"]],
                          lock_tx=(prev or {}).get("info"), lock_height=(prev or {}).get("height"), lock_time=(prev or {}).get("time"))
                events.append(ev)
    return p2sh_rows, spends, events
