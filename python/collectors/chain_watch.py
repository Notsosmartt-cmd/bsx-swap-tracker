#!/usr/bin/env python3
"""Live chain watch (2026-11-19): one long-lived connection per coin that
  coins: BTC, LTC (public peers, transactions and blocks), PART (the LOCAL particld's P2P port 51738, transactions and blocks; never its
  wallet RPC port), BCH (the local BCH daemon through the scan's four-method read-only allowlist, polled every 15 s: blocks only, no mempool).
  Lock outputs: P2WSH on BTC, LTC and PART; P2SH32 (OP_HASH256 <32> OP_EQUAL, BasicSwap bch.py getScriptDest) on BCH.  PART and BCH flags use
  the BTC-calibrated rules and are not validated yet.

  1. flags likely BasicSwap swap LOCKS when they appear, before the swap completes (early detection), with the four rules of the lock-time
     probe (docs/method.md; python/collectors/chain/chain_lock_early_detect.py; BTC precision 97 %, recall 74 % on 7 days):
       a P2WSH output whose value equals the coin-side amount of a public offer seen in the last 2 h (+-2 sat),
       in a transaction with version 2 and nLockTime 0, with at most 2 outputs, and a value that is not a multiple of 1,000 sat.
     It checks every unconfirmed transaction the peer relays (stage "mempool", minutes before mining) and every new block (stage "block").
     Each amount match is written with its rule results; flag=1 when all four hold.  LTC uses the BTC-calibrated rules (not validated on LTC).
  2. on a new flag, starts bsx-offer-match (at most once a minute) so data/analysis/offer_match_early.csv shows it within a minute.
  3. starts the regular scan unit (bsx-swap-tracker-p2p / bsx-swap-tracker-ltc) 45 s and 180 s after a new block, so completed swaps are stored within
     about a minute of the block instead of up to 10 minutes later (the 10-minute timers stay as the backup).

Read-only: the handshake announces no services and serves nothing; it asks peers to relay transactions (relay=1) and requests blocks and
transactions it is told about.  Blocks are checked (proof of work of the header, merkle root of the transactions) but this is not a validating
node: a flag is advisory, the scan re-verifies everything against the header chain.  Never use it to decide anything about funds.

Out (<data_dir>/): early_locks.csv (append; one row per amount match and stage), early_locks_latest.json (newest 100 rows),
chainwatch_status.json (heartbeat every 10 s; the watchdog restarts the unit when it is older than 3 min).
Offers: the public board snapshots in <snapshots_dir> (written every minute by the collector).

  python3 python/collectors/chain_watch.py                 run (bsx-chainwatch.service)
  python3 python/collectors/chain_watch.py --selftest      fake inputs only, no network
  python3 python/collectors/chain_watch.py --seconds 120   run for 2 minutes and print the status (foreground test)
"""
import argparse, csv, gzip, json, logging, os, random, socket, struct, subprocess, sys, threading, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _paths  # noqa: E401,E402  puts python/collectors/ and its sub-folders on the import path
import part_p2p  # noqa: E402
from collector_settings import S as SETTINGS  # noqa: E402
from collector_common import ROOT  # noqa: E402
import chainscan_lib as L  # noqa: E402
from p2p_blocks import NETS, P2PError, dsha, merkle_root, pack_msg, parse_version, pow_ok, version_payload, NODE_NETWORK, NODE_WITNESS  # noqa: E402

OUT = SETTINGS.data_dir
SNAP_DIR = SETTINGS.snapshots_dir
FLAGS_CSV, LATEST, STATUS = (os.path.join(OUT, n) for n in ("early_locks.csv", "early_locks_latest.json", "chainwatch_status.json"))
SCAN_UNIT = SETTINGS.scan_units                   # coin -> systemd user unit started after a block ([watch] in the settings file)
LOCK_TYPES = {"BTC": ("p2wsh",), "LTC": ("p2wsh",), "PART": ("p2wsh",), "BCH": ("p2sh32", "p2sh")}
TOL_SAT, OFFER_WINDOW_S = 2, 7200                  # probe: +-2 sat; offers live up to about 1.5 h before a lock (AFTER_S 5400) plus margin
MSG_TX, MSG_BLOCK, MSG_WITNESS_TX, MSG_WITNESS_BLOCK = 1, 2, 0x40000001, 0x40000002
TRIGGER_DELAYS = (45, 180)
SEEN_MAX = 100_000
COLS = ["seen_utc", "stage", "coin", "txid", "vout", "value_sat", "height", "flag", "rule_lt0_v2", "rule_max2_outputs", "rule_not_round",
        "n_offers", "offer_ids", "makers", "pairs", "tx_version", "tx_locktime", "n_outputs"]
log = logging.getLogger("chain_watch")


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ offers


class Offers:
    """amount (smallest unit of the coin) -> offers seen in the public board snapshots of the last OFFER_WINDOW_S seconds; incremental reload."""

    def __init__(self, snap_dir=SNAP_DIR, window=OFFER_WINDOW_S):
        self.dir, self.window = snap_dir, window
        self.files = {}                                   # name -> [(coin, amount, offer)]
        self.index = {}                                   # coin -> sorted list of (amount, offer_key)
        self.meta = {}                                    # offer_key -> dict
        self.loaded_at, self.lock = 0.0, threading.Lock()

    @staticmethod
    def _name_time(name):
        try:
            return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None

    def refresh(self, now=None):
        now = now or time.time()
        try:
            names = [n for n in os.listdir(self.dir) if n.endswith(".json.gz")]
        except OSError:
            names = []
        keep = {n for n in names if (self._name_time(n) or 0) >= now - self.window}
        for n in list(self.files):
            if n not in keep:
                del self.files[n]
        for n in sorted(keep - set(self.files)):
            rows = []
            try:
                with gzip.open(os.path.join(self.dir, n)) as f:
                    snap = json.load(f)
                for o in snap.get("offers") or []:
                    for side in ("from", "to"):
                        coin, amt = o.get("coin_" + side), o.get("amount_" + side)
                        if coin in SCAN_UNIT and isinstance(amt, int) and amt > 0:
                            rows.append((coin, amt, {"id": str(o.get("msg_id", ""))[-16:], "maker": o.get("addr_from", ""),
                                                     "pair": f"{o.get('coin_from')}/{o.get('coin_to')}"}))
            except Exception as e:                        # a half-written or bad snapshot: skip it, retry next refresh
                log.debug("snapshot %s skipped: %r", n, e)
                continue
            self.files[n] = rows
        idx, meta = {}, {}
        for rows in self.files.values():
            for coin, amt, o in rows:
                key = (coin, o["id"], amt)
                if key not in meta:
                    meta[key] = o
                    idx.setdefault(coin, []).append((amt, key))
        for v in idx.values():
            v.sort()
        with self.lock:
            self.index, self.meta, self.loaded_at = idx, meta, now
        return sum(len(v) for v in idx.values())

    def lookup(self, coin, value):
        import bisect
        with self.lock:
            lst = self.index.get(coin, [])
            lo = bisect.bisect_left(lst, (value - TOL_SAT,))
            out = []
            for amt, key in lst[lo:]:
                if amt > value + TOL_SAT:
                    break
                out.append(self.meta[key])
            return out


# ------------------------------------------------------------------ rules


def lock_matches(tx, coin, offers):
    """Rows for every P2WSH output of tx whose value equals a live offer amount; flag=1 when all four probe rules hold."""
    if tx.get("coinbase"):
        return []
    outs, rows = tx["vout"], []
    for i, o in enumerate(outs):
        if o.get("value") is None or L.out_type(o["script"])[0] not in LOCK_TYPES.get(coin, ("p2wsh",)):
            continue
        hits = offers.lookup(coin, o["value"])
        if not hits:
            continue
        r1 = tx["version"] == L.swap_tx_version("part" if coin == "PART" else "btc") and tx["locktime"] == 0
        r2 = len(outs) <= 2
        r3 = o["value"] % 1000 != 0
        rows.append({"txid": tx["txid"], "vout": i, "value_sat": o["value"], "flag": int(r1 and r2 and r3), "rule_lt0_v2": int(r1),
                     "rule_max2_outputs": int(r2), "rule_not_round": int(r3), "n_offers": len(hits),
                     "offer_ids": " ".join(sorted({h["id"] for h in hits})[:3]), "makers": " ".join(sorted({h["maker"] for h in hits})[:3]),
                     "pairs": " ".join(sorted({h["pair"] for h in hits})), "tx_version": tx["version"], "tx_locktime": tx["locktime"], "n_outputs": len(outs)})
    return rows


def coinbase_height(tx):
    """BIP34 height from the coinbase script (first push), or None."""
    try:
        s = tx["vin"][0]["script_sig"]                       # first push only: miners put arbitrary bytes after it
        return int.from_bytes(s[1:1 + s[0]], "little") if 1 <= s[0] <= 8 else None
    except Exception:
        return None


# ------------------------------------------------------------------ output


class Sink:
    def __init__(self):
        os.makedirs(OUT, exist_ok=True)
        self.lock, self.latest, self.done = threading.Lock(), [], set()
        self.on_flag = None                                  # called (outside the lock) when a newly written row has flag=1
        try:
            with open(LATEST) as f:
                self.latest = json.load(f).get("rows", [])
        except Exception:
            pass

    def write(self, coin, stage, rows, height=None):
        new = []
        with self.lock:
            for r in rows:
                key = (coin, stage, r["txid"], r["vout"])
                if key in self.done:
                    continue
                self.done.add(key)
                new.append({"seen_utc": utc(), "stage": stage, "coin": coin, "height": height if height is not None else "", **r})
            if not new:
                return 0
            if len(self.done) > SEEN_MAX:
                self.done.clear()
            fresh = not os.path.exists(FLAGS_CSV)
            with open(FLAGS_CSV, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLS)
                if fresh:
                    w.writeheader()
                for r in new:
                    w.writerow({k: r.get(k, "") for k in COLS})
            self.latest = (new[::-1] + self.latest)[:100]
            tmp = LATEST + f".{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"updated": utc(), "rows": self.latest}, f, indent=1)
            os.replace(tmp, LATEST)
        if self.on_flag and any(r["flag"] for r in new):
            try:
                self.on_flag()
            except Exception as e:
                log.warning("on_flag failed: %r", e)
        for r in new:
            (log.warning if r["flag"] else log.info)("%s %s %s:%d %d sat %s offers=%d makers=%s", "FLAG" if r["flag"] else "amount-match", coin,
                                                     r["txid"][:16], r["vout"], r["value_sat"], stage, r["n_offers"], r["makers"][:40])
        return len(new)


# ------------------------------------------------------------------ peer


def frame(buf, magic):
    """-> (cmd, payload, rest) for the first complete message in buf, or (None, None, buf).  Raises P2PError on garbage."""
    if len(buf) < 24:
        return None, None, buf
    if buf[:4] != magic:
        raise P2PError("wrong network magic")
    ln = struct.unpack("<I", buf[16:20])[0]
    if ln > 64 * 1024 * 1024:
        raise P2PError("oversized message")
    if len(buf) < 24 + ln:
        return None, None, buf
    payload = buf[24:24 + ln]
    if dsha(payload)[:4] != buf[20:24]:
        raise P2PError("bad checksum")
    return buf[4:16].rstrip(b"\0").decode("ascii", "replace"), payload, buf[24 + ln:]


def varint(b, o):
    v = b[o]
    if v < 0xFD:
        return v, o + 1
    n = {0xFD: 2, 0xFE: 4, 0xFF: 8}[v]
    return int.from_bytes(b[o + 1:o + 1 + n], "little"), o + 1 + n


def inv_items(payload):
    n, o = varint(payload, 0)
    out = []
    for _ in range(n):
        out.append((struct.unpack("<I", payload[o:o + 4])[0], payload[o + 4:o + 36]))
        o += 36
    return out


def getdata(items):
    n = len(items)
    head = bytes([n]) if n < 0xFD else b"\xfd" + struct.pack("<H", n)
    return head + b"".join(struct.pack("<I", t) + h for t, h in items)


class Watcher(threading.Thread):
    def __init__(self, coin, offers, sink, mempool=True, trigger=True):
        super().__init__(daemon=True, name="watch-" + coin)
        self.coin, self.net, self.offers, self.sink, self.mempool, self.trigger = coin, NETS[coin], offers, sink, mempool, trigger
        self.stop_ev = threading.Event()
        self.st = {"peer": None, "connected_since": None, "last_msg_utc": None, "blocks": 0, "last_block": None, "txs": 0, "tx_parse_errors": 0,
                   "amount_matches": 0, "flags": 0, "reconnects": 0, "last_error": None, "scan_triggers": 0}
        self.seen, self.bad, self.last_trigger = {}, set(), 0.0

    # -- connection
    def connect(self):
        names = list(self.net["seeds"])
        random.shuffle(names)
        for name in names:
            try:
                addrs = list({a[4][0] for a in socket.getaddrinfo(name, self.net["port"], socket.AF_INET, socket.SOCK_STREAM)})
            except OSError:
                continue
            random.shuffle(addrs)
            for ip in addrs[:10]:
                if ip in self.bad or self.stop_ev.is_set():
                    continue
                s = None
                try:
                    s = socket.create_connection((ip, self.net["port"]), timeout=15)
                    s.settimeout(15)
                    self.sock, self.buf = s, b""
                    self.send("version", version_payload()[:-1] + b"\x01")      # same as the scan's handshake, but relay=1 (we want tx announcements)
                    got_v = got_ack = False
                    end = time.time() + 20
                    while not (got_v and got_ack) and time.time() < end:
                        cmd, payload = self.recv(end - time.time())
                        if cmd == "version":
                            _, services = parse_version(payload)
                            if not (services & NODE_NETWORK and services & NODE_WITNESS):
                                raise P2PError("not a full witness node")
                            got_v = True
                            self.send("verack")
                        elif cmd == "verack":
                            got_ack = True
                    if got_v and got_ack:
                        self.st.update(peer=ip, connected_since=utc())
                        log.info("%s: connected to %s", self.coin, ip)
                        return
                    raise P2PError("handshake timed out")
                except (OSError, P2PError) as e:
                    self.bad.add(ip)
                    if s:
                        s.close()
                    log.debug("%s: %s refused: %r", self.coin, ip, e)
        if len(self.bad) > 200:
            self.bad.clear()
        raise P2PError("no peer reachable")

    def send(self, cmd, payload=b""):
        self.sock.sendall(pack_msg(self.net["magic"], cmd, payload))

    def recv(self, timeout):
        """next complete message, or (None, None) when nothing arrived within timeout; partial data stays buffered."""
        end = time.time() + max(0.1, timeout)
        while True:
            cmd, payload, self.buf = frame(self.buf, self.net["magic"])
            if cmd is not None:
                self.st["last_msg_utc"] = utc()
                self.last_msg = time.time()
                return cmd, payload
            left = end - time.time()
            if left <= 0:
                return None, None
            self.sock.settimeout(min(left, 15))
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            if not chunk:
                raise P2PError("peer closed the connection")
            self.buf += chunk

    # -- handling
    def on_block(self, payload):
        hdr = payload[:80]
        bh = dsha(hdr)
        if not pow_ok(hdr, self.net["scrypt"]):
            raise P2PError("block header fails its proof of work")
        _, txs = L.parse_raw_block(payload, self.net["family"])
        if merkle_root([t["txid"] for t in txs]) != hdr[36:68]:
            raise P2PError("merkle root mismatch")
        self.after_block(bh, txs, coinbase_height(txs[0]) if txs else None)

    def after_block(self, bh, txs, height):
        rows = [r for t in txs for r in lock_matches(t, self.coin, self.offers)]
        n = self.sink.write(self.coin, "block", rows, height)
        self.st["blocks"] += 1
        self.st["last_block"] = {"hash": bh[::-1].hex(), "height": height, "txs": len(txs), "at": utc(), "matches": len(rows)}
        self.st["amount_matches"] += n
        self.st["flags"] += sum(r["flag"] for r in rows)
        log.info("%s: block %s height %s, %d txs, %d amount matches", self.coin, bh[::-1].hex(), height, len(txs), len(rows))
        if self.trigger and time.time() - self.last_trigger > 30:
            self.last_trigger = time.time()
            for d in TRIGGER_DELAYS:
                threading.Timer(d, self.start_scan).start()

    def start_scan(self):
        if self.stop_ev.is_set():
            return
        r = subprocess.run(["systemctl", "--user", "start", "--no-block", SCAN_UNIT[self.coin]], capture_output=True, text=True, timeout=30)
        self.st["scan_triggers"] += 1
        if r.returncode:
            log.warning("%s: could not start %s: %s", self.coin, SCAN_UNIT[self.coin], r.stderr.strip()[:200])

    def on_tx(self, payload):
        try:
            tx = L.parse_tx(L.Reader(payload), self.net["family"])
        except Exception:
            self.st["tx_parse_errors"] += 1                        # e.g. LTC MWEB transactions
            return
        self.st["txs"] += 1
        rows = lock_matches(tx, self.coin, self.offers)
        if rows:
            self.st["amount_matches"] += self.sink.write(self.coin, "mempool", rows)
            self.st["flags"] += sum(r["flag"] for r in rows)

    def session(self):
        self.connect()
        self.last_msg, last_ping = time.time(), time.time()
        while not self.stop_ev.is_set():
            cmd, payload = self.recv(10)
            now = time.time()
            if now - last_ping > 300:
                self.send("ping", struct.pack("<Q", random.getrandbits(64)))
                last_ping = now
            if now - self.last_msg > 900:
                raise P2PError("peer silent for 15 min")
            if cmd is None:
                continue
            if cmd == "ping":
                self.send("pong", payload)
            elif cmd == "inv":
                want = []
                for t, h in inv_items(payload):
                    if t in (MSG_BLOCK, MSG_WITNESS_BLOCK):
                        want.append((MSG_WITNESS_BLOCK, h))
                    elif t in (MSG_TX, MSG_WITNESS_TX) and self.mempool and h not in self.seen:
                        self.seen[h] = None
                        want.append((MSG_WITNESS_TX, h))
                if len(self.seen) > SEEN_MAX:
                    for k in list(self.seen)[:SEEN_MAX // 2]:
                        del self.seen[k]
                if want:
                    self.send("getdata", getdata(want[:1000]))
            elif cmd == "headers":                                  # peers that announce blocks by headers
                n, o = varint(payload, 0)
                hs = [dsha(payload[o + i * 81:o + i * 81 + 80]) for i in range(n)]
                if hs:
                    self.send("getdata", getdata([(MSG_WITNESS_BLOCK, h) for h in hs[-8:]]))
            elif cmd == "block":
                self.on_block(payload)
            elif cmd == "tx":
                self.on_tx(payload)

    def run(self):
        backoff = 5
        while not self.stop_ev.is_set():
            try:
                self.session()
            except (OSError, P2PError, ValueError, IndexError, struct.error) as e:
                self.st["last_error"] = f"{utc()} {e!r}"[:200]
                log.warning("%s: %r; reconnecting in %d s", self.coin, e, backoff)
            finally:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.st.update(peer=None, connected_since=None)
            if self.stop_ev.wait(backoff):
                break
            self.st["reconnects"] += 1
            backoff = min(120, backoff * 2) if not self.st.get("last_msg_utc") else 5


class PartWatcher(Watcher):
    """PART from the local particld's P2P port (relay on): same loop as BTC/LTC, Particl headers (112 bytes, proof of stake: no work check)."""

    def __init__(self, offers, sink, mempool=True, trigger=True):
        threading.Thread.__init__(self, daemon=True, name="watch-PART")
        self.coin, self.offers, self.sink, self.mempool, self.trigger = "PART", offers, sink, mempool, trigger
        self.net = dict(part_p2p.NET, scrypt=False)
        self.stop_ev = threading.Event()
        self.st = {"peer": None, "connected_since": None, "last_msg_utc": None, "blocks": 0, "last_block": None, "txs": 0, "tx_parse_errors": 0,
                   "amount_matches": 0, "flags": 0, "reconnects": 0, "last_error": None, "scan_triggers": 0}
        self.seen, self.bad, self.last_trigger = {}, set(), 0.0

    def connect(self):
        s = socket.create_connection(("127.0.0.1", part_p2p.NET["port"]), timeout=15)
        self.sock, self.buf = s, b""
        self.send("version", part_p2p.version_payload(relay=True))
        got_v = got_ack = False
        end = time.time() + 20
        while not (got_v and got_ack) and time.time() < end:
            cmd, payload = self.recv(end - time.time())
            if cmd == "version":
                got_v = True
                self.send("verack")
            elif cmd == "verack":
                got_ack = True
        if not (got_v and got_ack):
            s.close()
            raise P2PError("PART handshake timed out")
        self.st.update(peer="127.0.0.1", connected_since=utc())
        log.info("PART: connected to the local particld")

    def on_block(self, payload):
        _, txs = part_p2p.check_block(payload, dsha(payload[:112]))
        self.after_block(dsha(payload[:112]), txs, None)


class RpcBlockWatcher(threading.Thread):
    """BCH: polls the local daemon's best block (allowlisted read-only RPC of chain_swap_scan) every 15 s; block-stage flags and scan trigger."""

    def __init__(self, coin, offers, sink, trigger=True):
        super().__init__(daemon=True, name="watch-" + coin)
        self.coin, self.offers, self.sink, self.trigger = coin, offers, sink, trigger
        self.stop_ev = threading.Event()
        self.st = {"peer": None, "connected_since": None, "last_msg_utc": None, "blocks": 0, "last_block": None, "txs": 0, "tx_parse_errors": 0,
                   "amount_matches": 0, "flags": 0, "reconnects": 0, "last_error": None, "scan_triggers": 0}
        self.last_trigger = 0.0

    def run(self):
        import chain_swap_scan as CS
        rpc, best = None, None
        while not self.stop_ev.is_set():
            try:
                if rpc is None:
                    host, port, user, pw = CS.load_credentials(self.coin)
                    rpc = CS.ChainRPC(self.coin, host, port, user, pw, timeout=60)
                    best = rpc.call("getbestblockhash")              # start from the current tip: the scan covers the history
                    self.st.update(peer="local daemon", connected_since=utc())
                h = rpc.call("getbestblockhash")
                self.st["last_msg_utc"] = utc()
                if h != best:
                    best = h
                    raw = bytes.fromhex(rpc.call("getblock", [h, 0]))
                    if dsha(raw[:80])[::-1].hex() != h:
                        raise P2PError("daemon block does not hash to its id")
                    _, txs = L.parse_raw_block(raw, CS.COINS[self.coin]["family"])
                    if merkle_root([t["txid"] for t in txs]) != raw[36:68]:
                        raise P2PError("merkle root mismatch")
                    Watcher.after_block(self, bytes.fromhex(h)[::-1], txs, coinbase_height(txs[0]) if txs else None)
            except Exception as e:                                       # daemon restarting, RPC refused: retry, never crash the watcher
                self.st["last_error"] = f"{utc()} {e!r}"[:200]
                log.warning("%s: %r", self.coin, e)
                rpc = None
                self.st["peer"] = None
            self.stop_ev.wait(15)

    def start_scan(self):
        Watcher.start_scan(self)


# ------------------------------------------------------------------ main


def write_status(watchers, offers, started):
    doc = {"updated": utc(), "pid": os.getpid(), "started": started, "offers_amounts": sum(len(v) for v in offers.index.values()),
           "offers_loaded_age_s": round(time.time() - offers.loaded_at), "snapshots": len(offers.files),
           "coins": {w.coin: w.st for w in watchers}}
    tmp = STATUS + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, STATUS)
    return doc


def selftest():
    class FakeOffers(Offers):
        def __init__(self, amounts):
            super().__init__(snap_dir="/nonexistent")
            self.index = {"BTC": sorted((a, ("BTC", f"id{a}", a)) for a in amounts)}
            self.meta = {("BTC", f"id{a}", a): {"id": f"id{a}", "maker": "PmakerX", "pair": "BTC/XMR"} for a in amounts}
    p2wsh, p2wpkh = b"\x00\x20" + b"\x11" * 32, b"\x00\x14" + b"\x22" * 20
    off = FakeOffers([1_634_709, 5_000_000])
    tx = {"txid": "aa" * 32, "version": 2, "locktime": 0, "coinbase": False, "vin": [], "vout": [{"value": 1_634_710, "script": p2wsh}, {"value": 99, "script": p2wpkh}]}
    r = lock_matches(tx, "BTC", off)
    assert len(r) == 1 and r[0]["flag"] == 1 and r[0]["n_offers"] == 1, r            # within 2 sat, all rules
    assert lock_matches({**tx, "locktime": 870000}, "BTC", off)[0]["flag"] == 0       # wallet-style nLockTime: match but no flag
    assert lock_matches({**tx, "vout": tx["vout"] + [{"value": 1, "script": p2wpkh}]}, "BTC", off)[0]["rule_max2_outputs"] == 0
    assert lock_matches({**tx, "vout": [{"value": 5_000_000, "script": p2wsh}]}, "BTC", off)[0]["rule_not_round"] == 0
    assert lock_matches({**tx, "vout": [{"value": 1_634_712, "script": p2wsh}]}, "BTC", off) == []      # 3 sat off: no match
    assert lock_matches({**tx, "vout": [{"value": 1_634_709, "script": p2wpkh}]}, "BTC", off) == []     # not P2WSH
    assert lock_matches({**tx, "coinbase": True}, "BTC", off) == []
    p2sh32 = b"\xaa\x20" + b"\x55" * 32 + b"\x87"
    offb = FakeOffers([])
    offb.index = {"BCH": [(7_654_321, ("BCH", "idb", 7_654_321))]}; offb.meta = {("BCH", "idb", 7_654_321): {"id": "idb", "maker": "Pm", "pair": "BCH/XMR"}}
    assert lock_matches({**tx, "vout": [{"value": 7_654_321, "script": p2sh32}]}, "BCH", offb)[0]["flag"] == 1          # BCH lock is P2SH32
    assert lock_matches({**tx, "vout": [{"value": 7_654_321, "script": p2wsh}]}, "BCH", offb) == []                    # not a BCH lock type
    assert lock_matches({**tx, "vout": [{"value": None, "script": p2wsh}]}, "BTC", off) == []                          # hidden PART value
    m = NETS["BTC"]["magic"]
    raw = pack_msg(m, "ping", b"12345678") + pack_msg(m, "inv", b"\x01" + struct.pack("<I", 2) + b"\x33" * 32)
    c1, p1, rest = frame(raw[:30], m)
    assert c1 is None and rest == raw[:30]                                         # partial message stays buffered
    c1, p1, rest = frame(raw, m)
    c2, p2, rest = frame(rest, m)
    assert (c1, p1, c2, rest) == ("ping", b"12345678", "inv", b"") and inv_items(p2) == [(2, b"\x33" * 32)]
    assert getdata([(MSG_WITNESS_TX, b"\x44" * 32)])[:5] == b"\x01" + struct.pack("<I", MSG_WITNESS_TX)
    cb = {"vin": [{"script_sig": bytes([3]) + (870123).to_bytes(3, "little") + b"\xfa\xbe/miner tag/\xff"}]}
    assert coinbase_height(cb) == 870123
    log.info("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,LTC,PART,BCH")
    ap.add_argument("--config", help="settings file (paths, protected ports)")
    ap.add_argument("--no-mempool", action="store_true", help="blocks only (no transaction relay)")
    ap.add_argument("--no-trigger", action="store_true", help="do not start the scan units after a block")
    ap.add_argument("--seconds", type=int, default=0, help="run this long, print the status, exit (foreground test)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [chain_watch] %(message)s")
    selftest()
    if a.selftest:
        return 0
    offers, sink = Offers(), Sink()
    last_match = [0.0]

    def match_now():                                        # offer-match a new flag at once (bsx-offer-match writes offer_match_early.csv for the desk)
        if not a.no_trigger and time.time() - last_match[0] > 60:
            last_match[0] = time.time()
            subprocess.run(["systemctl", "--user", "start", "--no-block", SETTINGS.match_unit], capture_output=True, timeout=30)
    sink.on_flag = match_now
    log.info("offers: %d amounts from %d snapshots", offers.refresh(), len(offers.files))
    watchers = []
    for c in a.coins.split(","):
        if c == "PART":
            watchers.append(PartWatcher(offers, sink, mempool=not a.no_mempool, trigger=not a.no_trigger))
        elif c == "BCH":
            watchers.append(RpcBlockWatcher("BCH", offers, sink, trigger=not a.no_trigger))
        else:
            watchers.append(Watcher(c, offers, sink, mempool=not a.no_mempool, trigger=not a.no_trigger))
    for w in watchers:
        w.start()
    started, t0, last_off = utc(), time.time(), time.time()
    try:
        while True:
            time.sleep(10)
            if time.time() - last_off > 60:
                offers.refresh()
                last_off = time.time()
            doc = write_status(watchers, offers, started)
            if a.seconds and time.time() - t0 >= a.seconds:
                print(json.dumps(doc, indent=1))
                break
    except KeyboardInterrupt:
        pass
    for w in watchers:
        w.stop_ev.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
