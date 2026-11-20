"""Read-only on-chain scan for BasicSwap swaps (plan: docs/method.md; report: python/collectors/chain/chain_swap_report.py).

Reads confirmed blocks only, from the local chain daemons, through a FOUR-METHOD allowlist (getblockcount, getblockhash, getbestblockhash,
getblock).  It never calls a wallet method, never uses a /wallet/ path, never signs or sends anything, and refuses any port that is not the
expected chain-daemon port of the coin.  Credentials are read at run time from the node's own configuration, held in memory, and never printed
or logged.  Output: <data_dir>/chainscan.sqlite (+ status.json), logs in data/logs/chain_swap_scan.log.

  python3 python/collectors/chain_swap_scan.py --probe                  # one connectivity test per coin (getblockcount + tip time)
  python3 python/collectors/chain_swap_scan.py --coins DASH,DOGE,BCH    # scan, resumable, until caught up or --max-seconds
"""
import argparse
import http.client
import json
import os
import re
import signal
import sqlite3
import statistics
import sys
import time
from base64 import b64encode
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector_common import DATA, ROOT, setup_logging, single_instance  # noqa: E402
import chainscan_lib as L  # noqa: E402
import p2p_blocks as P2P  # noqa: E402
from collector_settings import S as SETTINGS  # noqa: E402  paths and protected ports (deploy/bsx-swap-tracker.toml)
import part_p2p as PART  # noqa: E402  Particl from the local particld's P2P port (2026-11-19)

OUT_DIR = SETTINGS.data_dir
DB_PATH = os.path.join(OUT_DIR, "chainscan.sqlite")
STATUS_PATH = os.path.join(OUT_DIR, "status.json")
BASICSWAP_JSON = SETTINGS.basicswap_json

ALLOWED_METHODS = frozenset({"getblockcount", "getblockhash", "getbestblockhash", "getblock"})
# wallet RPC and UI ports the tool must never contact, whatever a config file says
PROTECTED_PORTS = SETTINGS.protected_ports   # BasicSwap's defaults + the host's [safety] protected_ports

# coin -> how to read it.  `port` is the ONLY port the tool will talk to for that coin.
COINS = {
    "DASH": dict(family="dash", mode="raw", port=9998, cred="basicswap:dash", bpd=576),
    "DOGE": dict(family="doge", mode="raw", port=42069, cred="basicswap:dogecoin", bpd=1440),
    "BCH": dict(family="bch", mode="raw", port=19997, cred="basicswap:bitcoincash", bpd=144),
    "DCR": dict(family="dcr", mode="dcr", port=9109, cred="basicswap:decred", bpd=288),
    # BTC and LTC have no local chain and we do NOT sync one (a pruned node still downloads the whole chain): selected recent blocks come straight from the
    # peer-to-peer network (python/collectors/p2p_blocks.py), authenticated against the header chain the BasicSwap node already keeps; no prevout data on this path.
    "BTC": dict(family="btc", mode="p2p", bpd=144, rps=10),
    "LTC": dict(family="ltc", mode="p2p", bpd=576, rps=40),
    # PART (2026-11-19): BasicSwap puts its P2WSH lock on PART for PART offers.  Blocks come from the LOCAL particld over its P2P port 51738 (never the RPC
    # port 19792, a protected wallet port); python/collectors/part_p2p.py checks hash and merkle root of every block.  Local and fast, so the cap is generous.
    "PART": dict(family="part", mode="part_p2p", bpd=720, rps=60),
}
DEFAULT_COINS = "BTC,LTC,BCH,PART"
# Retired from the scan on 2026-11-16 (docs/method.md): BasicSwap never puts its script lock on these chains, so no BasicSwap swap can be seen there.
# DASH and DOGE: no segwit / scriptless, always the key-split side (basicswap.py is_reverse_ads_bid, validateSwapType); DCR: could hold the lock for a DCR-from offer but the board
# has had 0 DCR offers in 192,334 (the DCR events are Decred's own DEX).  Their stored history stays in the database; they can still be scanned by hand with --coins.
RETIRED = {"DASH": "never holds the script lock (no segwit)", "DOGE": "never holds the script lock (scriptless side)", "DCR": "no DCR offers on the board; events are Decred DEX"}
SYNCED_MAX_AGE_S = 36 * 3600
P2SH_RETAIN_DAYS = {"BTC": 7, "LTC": 7}   # days of script-hash outputs kept (default 21): BTC has about 200k such outputs a day, disk
P2SH_RETAIN_DEFAULT = 21
SPENDS_RETAIN_DAYS = {}   # no age cap on script_spends (deep history for modelling); {"BTC": 90} would restore a rolling cap
# how far back each coin is scanned by default; 0 = as far as the source allows (BTC/LTC: the header window, DASH/DOGE/BCH: what the local node kept).
# DCR has its whole chain locally (1.1 million blocks, years of mostly other software) so it is limited to 180 days.
DEPTH_DAYS = {"DCR": 180, "PART": 180}   # PART: 180 days at first (the header store keeps 400)
WARMUP_DAYS = 2   # BTC/LTC: when a range resumes where the script-hash index has been pruned, re-index this many days before it so locks just before the start resolve
WIDE_MAX_HEX = {"BTC": 140, "LTC": 140}      # store script bytes only up to 140 bytes there (covers the 94-byte HTLC, 71-byte lock, 115-byte refund script)
REORG_DEPTH = 8
FIRST_SCAN_DAYS = 7.0         # first scan without --backfill-days
MAX_LAG_H = 3.0   # hours: newest stored block may be this old and the coin still counts as complete to the tip (polling every 10 minutes, slow blocks)


log = None
HEARTBEAT = None        # path of this instance's heartbeat file (set in main); the external watchdog restarts a run whose heartbeat goes stale
_last_beat = 0.0


def beat(coin=None, height=None, note=""):
    """write the heartbeat (at most every 10 s): proof that the loop is still turning, read by a watchdog"""
    global _last_beat
    if not HEARTBEAT or time.time() - _last_beat < 10:
        return
    _last_beat = time.time()
    try:
        with open(HEARTBEAT + ".tmp", "w") as f:
            json.dump({"ts": int(_last_beat), "pid": os.getpid(), "coin": coin, "height": height, "note": note}, f)
        os.replace(HEARTBEAT + ".tmp", HEARTBEAT)
    except OSError:
        pass


class RPCNotAllowed(Exception):
    pass


class BlockPruned(Exception):
    pass


class RPCUnavailable(Exception):
    pass


# ---------------------------------------------------------------- credentials (never printed)


def load_credentials(coin):
    cfg = COINS[coin]
    kind, _, ref = cfg["cred"].partition(":")
    if kind == "basicswap":
        with open(BASICSWAP_JSON) as f:
            cc = json.load(f)["chainclients"][ref]
        host, port, user, pw = cc.get("rpchost", "127.0.0.1"), int(cc["rpcport"]), cc["rpcuser"], cc["rpcpassword"]
    else:
        path = os.path.join(ROOT, ref)
        kv = {}
        with open(path) as f:
            for line in f:
                m = re.match(r"^\s*(rpcuser|rpcpassword|rpcport)\s*=\s*(.*?)\s*$", line)
                if m:
                    kv[m.group(1)] = m.group(2)
        host, port, user, pw = "127.0.0.1", int(kv["rpcport"]), kv["rpcuser"], kv["rpcpassword"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RPCNotAllowed("%s: RPC host is not loopback" % coin)
    if port != cfg["port"] or port in PROTECTED_PORTS:
        raise RPCNotAllowed("%s: configured RPC port does not match the expected chain-daemon port" % coin)
    return host, port, user, pw


class ChainRPC:
    """Allowlisted JSON-RPC client.  Raises RPCNotAllowed BEFORE any request for any other method or for odd getblock parameters."""

    def __init__(self, coin, host, port, user, password, timeout=180):
        self.coin, self.host, self.port, self.timeout = coin, host, port, timeout
        self._auth = "Basic " + b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.lat = []

    @staticmethod
    def check(method, params):
        if method not in ALLOWED_METHODS:
            raise RPCNotAllowed("method %r is not on the allowlist" % (method,))
        if method == "getblock":
            ok = (len(params) in (2, 3) and isinstance(params[0], str) and re.fullmatch(r"[0-9a-f]{64}", params[0]) is not None
                  and (params[1] in (0, 1, 2, 3) or params[1] is True or params[1] is False) and (len(params) == 2 or params[2] is True))
            if not ok:
                raise RPCNotAllowed("getblock parameters outside the allowed shapes")
        elif method == "getblockhash":
            if not (len(params) == 1 and isinstance(params[0], int) and params[0] >= 0):
                raise RPCNotAllowed("getblockhash parameter")
        elif params:
            raise RPCNotAllowed("%s takes no parameters" % method)

    def call(self, method, params=()):
        params = list(params)
        self.check(method, params)
        body = json.dumps({"jsonrpc": "1.0", "id": "chainscan", "method": method, "params": params})
        t0 = time.time()
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.request("POST", "/", body, {"Content-Type": "application/json", "Authorization": self._auth})   # root path only, never /wallet/...
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            raise RPCUnavailable("%s: %s" % (self.coin, type(e).__name__))
        self.lat.append(time.time() - t0)
        del self.lat[:-50]
        try:
            msg = json.loads(data, parse_float=Decimal)
        except ValueError:
            raise RPCUnavailable("%s: unreadable response (HTTP %s)" % (self.coin, resp.status))
        if msg.get("error"):
            e = msg["error"]
            text = str(e.get("message", "")) if isinstance(e, dict) else str(e)
            if "pruned" in text.lower() or "undo data" in text.lower() or "not available" in text.lower():
                raise BlockPruned(text)
            if isinstance(e, dict) and e.get("code") == -28:
                raise RPCUnavailable("%s: node is starting (%s)" % (self.coin, text))
            raise RPCUnavailable("%s: RPC error %s" % (self.coin, text[:80]))
        return msg["result"]


# ---------------------------------------------------------------- database

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS scan_state(coin TEXT PRIMARY KEY, last_height INTEGER, last_hash TEXT, last_time INTEGER, first_height INTEGER, state TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS blocks(coin TEXT, height INTEGER, hash TEXT, time INTEGER, ntx INTEGER, prev TEXT, PRIMARY KEY(coin, height));
CREATE TABLE IF NOT EXISTS p2sh_outputs(coin TEXT, txid TEXT, vout INTEGER, value INTEGER, otype TEXT, program TEXT, height INTEGER, time INTEGER, txinfo TEXT, PRIMARY KEY(coin, txid, vout));
CREATE INDEX IF NOT EXISTS p2sh_height ON p2sh_outputs(coin, height);
CREATE TABLE IF NOT EXISTS script_spends(coin TEXT, height INTEGER, time INTEGER, txid TEXT, vin INTEGER, script_sha256 TEXT, script_len INTEGER, script_hex TEXT,
  nwit INTEGER, nin INTEGER, nout INTEGER, prev_txid TEXT, prev_vout INTEGER, prev_value INTEGER, PRIMARY KEY(coin, txid, vin));
CREATE INDEX IF NOT EXISTS spends_sha ON script_spends(script_sha256);
CREATE TABLE IF NOT EXISTS swap_events(id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, height INTEGER, block_hash TEXT, block_time INTEGER, txid TEXT, vin INTEGER,
  kind TEXT, protocol TEXT, confidence TEXT, secret_hash TEXT, secret TEXT, lock_val INTEGER, script_sha256 TEXT, amount_sat INTEGER, prev_txid TEXT, prev_vout INTEGER,
  sequence INTEGER, tx_nin INTEGER, tx_nout INTEGER, tx_version INTEGER, tx_locktime INTEGER, outputs TEXT, lock_tx TEXT, lock_height INTEGER, lock_time INTEGER, extra TEXT,
  UNIQUE(coin, txid, vin, kind));
CREATE INDEX IF NOT EXISTS events_hash ON swap_events(secret_hash);
CREATE INDEX IF NOT EXISTS events_time ON swap_events(coin, block_time);
CREATE TABLE IF NOT EXISTS backfill(coin TEXT PRIMARY KEY, next_h INTEGER, end_h INTEGER);
CREATE TABLE IF NOT EXISTS gaps(coin TEXT, from_height INTEGER, to_height INTEGER, reason TEXT, ts INTEGER);
"""


def open_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=180)
    db.executescript(SCHEMA)
    db.execute("PRAGMA synchronous=NORMAL")          # WAL: a power cut can lose the last few seconds, never corrupt; the scan is resumable
    db.execute("PRAGMA cache_size=-65536")
    db.execute("PRAGMA wal_autocheckpoint=20000")    # checkpoint every 80 MB of WAL instead of every 4 MB: fewer random page write-backs while three writers append           # 64 MB page cache for the lookups of spends against the script-hash index
    return db


def insert_block(db, coin, height, block_hash, header, txs, p2sh_rows, spends, events):
    db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?)", (coin, height, block_hash, header["time"], len(txs), header.get("prev")))
    db.executemany("INSERT OR REPLACE INTO p2sh_outputs VALUES (?,?,?,?,?,?,?,?,?)", p2sh_rows)
    db.executemany("INSERT OR IGNORE INTO script_spends VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", spends)
    for e in events:
        db.execute("INSERT OR IGNORE INTO swap_events(coin,height,block_hash,block_time,txid,vin,kind,protocol,confidence,secret_hash,secret,lock_val,script_sha256,amount_sat,prev_txid,prev_vout,"
                   "sequence,tx_nin,tx_nout,tx_version,tx_locktime,outputs,lock_tx,lock_height,lock_time,extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (e["coin"], e["height"], e["block_hash"], e["block_time"], e["txid"], e["vin"], e["kind"], e["protocol"], e["confidence"], e.get("secret_hash"), e.get("secret"),
                    e.get("lock_val"), e["script_sha256"], e["amount_sat"], e["prev_txid"], e["prev_vout"], e["sequence"], e["tx_nin"], e["tx_nout"], e["tx_version"],
                    e["tx_locktime"], json.dumps(e["outputs"]), json.dumps(e["lock_tx"]) if e["lock_tx"] else None, e["lock_height"], e["lock_time"], json.dumps(e.get("extra") or {})))
        if e["kind"] == "adaptor_refund_spend":      # the refund output was spent: its creating transaction is certainly a lock-refund transaction
            db.execute("UPDATE swap_events SET confidence='certain', kind='adaptor_refund_tx' WHERE coin=? AND txid=? AND kind IN ('adaptor_refund_tx','adaptor_lock_spend')", (coin, e["prev_txid"]))
        elif e["kind"] == "adaptor_refund_tx":       # the spend may already be stored (an older range scanned after the tip): upgrade now (2026-11-20)
            db.execute(REFUND_SPENT_UPGRADE, (coin, e["txid"], coin, e["block_time"], e["txid"]))


def db_retry(db, fn, tries=40):
    """the two scanner services share one SQLite file: when the other one holds the write lock, wait and redo the whole unit of work"""
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            db.rollback()
            time.sleep(min(10.0, 0.5 * (attempt + 1)))
    raise sqlite3.OperationalError("database stayed locked")


def make_lookup(db, coin):
    def lookup(txid, vout):
        r = db.execute("SELECT value, otype, program, height, time, txinfo FROM p2sh_outputs WHERE coin=? AND txid=? AND vout=?", (coin, txid, vout)).fetchone()
        if not r:
            return None
        return {"value": r[0], "otype": r[1], "program": r[2], "height": r[3], "time": r[4], "info": json.loads(r[5]) if r[5] else None}
    return lookup


# a refund tx is 'certain' only while a refund spend of its output is stored (review item 9, 2026-11-20; same SQL in cpp/src/store.cpp)
REFUND_SPENT_UPGRADE = ("UPDATE swap_events SET confidence='certain' WHERE coin=? AND txid=? AND kind='adaptor_refund_tx' AND EXISTS "
                        "(SELECT 1 FROM swap_events s WHERE s.coin=? AND s.block_time>=? AND s.kind='adaptor_refund_spend' AND s.prev_txid=?)")
REFUND_UNSPENT_DOWNGRADE = ("UPDATE swap_events SET confidence='likely' WHERE coin=? AND kind='adaptor_refund_tx' AND confidence='certain' AND NOT EXISTS "
                            "(SELECT 1 FROM swap_events s WHERE s.coin=swap_events.coin AND s.block_time>=swap_events.block_time "
                            "AND s.kind='adaptor_refund_spend' AND s.prev_txid=swap_events.txid)")


def rewind(db, coin, from_height):
    for t in ("blocks", "p2sh_outputs", "script_spends", "swap_events"):
        db.execute("DELETE FROM %s WHERE coin=? AND height>=?" % t, (coin, from_height))
    db.execute(REFUND_UNSPENT_DOWNGRADE, (coin,))      # an upgrade made by a refund spend in the rewound blocks is undone (2026-11-20)
    db.commit()


# ---------------------------------------------------------------- fetching


def fetch_block(rpc, cfg, block_hash):
    mode, family = cfg["mode"], cfg["family"]
    if mode == "raw":
        raw = bytes.fromhex(rpc.call("getblock", [block_hash, 0]))
        return L.parse_raw_block(raw, family)
    if mode == "json3":
        return L.norm_json_block(rpc.call("getblock", [block_hash, 3]), family)
    if mode == "json2":
        return L.norm_json_block(rpc.call("getblock", [block_hash, 2]), family)
    if mode == "dcr":
        return L.norm_json_block(rpc.call("getblock", [block_hash, True, True]), "dcr")
    raise ValueError(mode)


def header_time(rpc, cfg, block_hash):
    r = rpc.call("getblock", [block_hash, True if cfg["mode"] == "dcr" else 1])
    return int(r["time"])


def earliest_available(rpc, cfg, lo, hi):
    """lowest height whose block data is still on disk (pruned nodes); availability is monotonic, so bisect with cheap header-level calls"""
    def avail(h):
        try:
            rpc.call("getblock", [rpc.call("getblockhash", [h]), True if cfg["mode"] == "dcr" else 1])
            return True
        except BlockPruned:
            return False
    if avail(lo):
        return lo
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if avail(mid):
            hi = mid
        else:
            lo = mid
    return hi


class Throttle:
    def __init__(self, rps):
        self.gap, self.last = 1.0 / rps, 0.0
        self.base = None

    def wait(self):
        d = self.last + self.gap - time.time()
        if d > 0:
            time.sleep(d)
        self.last = time.time()

    def check(self, rpc):
        """latency tripwire: the live daemon must not feel us.  True = back off."""
        if len(rpc.lat) < 20:
            return False
        if self.base is None:
            self.base = max(statistics.median(rpc.lat), 0.05)
            return False
        med = statistics.median(rpc.lat[-20:])
        return med > max(3 * self.base, 3.0)


STOP = False


def _sigterm(*_):
    global STOP
    STOP = True


class RpcSource:
    """block source = one local chain daemon behind the four-method allowlist"""

    def __init__(self, coin, cfg):
        host, port, user, pw = load_credentials(coin)
        self.rpc, self.cfg = ChainRPC(coin, host, port, user, pw), cfg

    @property
    def lat(self):
        return self.rpc.lat

    def tip(self):
        return self.rpc.call("getblockcount")

    def tip_time(self):
        return header_time(self.rpc, self.cfg, self.rpc.call("getbestblockhash"))

    def earliest(self, want, tip):
        return earliest_available(self.rpc, self.cfg, want, tip)

    def blockhash(self, h):
        return self.rpc.call("getblockhash", [h])

    def block(self, h, bh):
        return fetch_block(self.rpc, self.cfg, bh)


class P2PAdapter:
    """block source = peer-to-peer fetch of selected blocks (BTC, LTC); peer and header problems surface as RPCUnavailable so the scan loop treats them alike"""

    def __init__(self, coin):
        self.src = P2P.P2PSource(coin, log)

    @property
    def lat(self):
        return self.src.lat

    def _w(self, fn, *a):
        try:
            return fn(*a)
        except (OSError, P2P.P2PError) as e:
            raise RPCUnavailable("%s: %s" % (self.src.coin, e))

    def close(self):
        self.src.close()

    def tip(self):
        return self._w(self.src.tip)

    def tip_time(self):
        return self._w(self.src.tip_time)

    def earliest(self, want, tip):
        return self.src.earliest(want, tip)

    def blockhash(self, h):
        return self._w(self.src.blockhash, h)

    def block(self, h, bh):
        return self._w(self.src.block, h, bh)


class PartAdapter(P2PAdapter):
    """block source = the local particld's P2P port (python/collectors/part_p2p.py); errors surface as RPCUnavailable like the others"""

    def __init__(self, coin):
        try:
            self.src = PART.PartSource(log)
        except (OSError, P2P.P2PError) as e:
            raise RPCUnavailable("PART: %s" % e)
        self.src.coin = coin


def make_source(coin, cfg):
    if cfg["mode"] == "part_p2p":
        return PartAdapter(coin)
    return P2PAdapter(coin) if cfg["mode"] == "p2p" else RpcSource(coin, cfg)


def in_back_pending(db, coin):
    r = db.execute("SELECT next_h, end_h FROM backfill WHERE coin=?", (coin,)).fetchone()
    return bool(r and r[0] <= r[1])


def warm_up(db, src, coin, cfg, start, thr, t_end):
    """BTC/LTC prune the script-hash index quickly (7 days); a range that resumes later than that has lost the outputs just before its start, so a lock created
    there could not be resolved.  Re-index the WARMUP_DAYS before the start (index rows only; nothing else is written).  Idempotent."""
    mn = db.execute("SELECT MIN(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
    if mn is None or start <= mn:
        return
    lo = max(mn, start - int(cfg["bpd"] * WARMUP_DAYS))
    have = db.execute("SELECT COUNT(*) FROM p2sh_outputs WHERE coin=? AND height BETWEEN ? AND ?", (coin, lo, start - 1)).fetchone()[0]
    if have > 0:
        return
    log.info("%s: re-indexing %d blocks before height %d (script-hash index was pruned there)", coin, start - lo, start)
    for h in range(lo, start):
        if STOP or time.time() > t_end:
            break
        beat(coin, h, "warm-up")
        thr.wait()
        bh = src.blockhash(h)
        header, txs = src.block(h, bh)
        rows, _, _ = L.scan_block(coin, cfg["family"], h, bh, header, txs, lambda a, b: None, wide=False, index_only=True)
        db_retry(db, lambda: db.executemany("INSERT OR REPLACE INTO p2sh_outputs VALUES (?,?,?,?,?,?,?,?,?)", rows))
    db_retry(db, db.commit)


def scan_coin(db, coin, args, budget_s):
    """-> state string: caught_up | partial | syncing | unreachable | error  (closes the block source, and with it the read-ahead threads, when done)"""
    holder = []
    try:
        return _scan_coin(db, coin, args, budget_s, holder)
    finally:
        for src in holder:
            close = getattr(src, "close", None)
            if close:
                try:
                    close()
                except Exception:      # noqa: BLE001
                    pass


def _scan_coin(db, coin, args, budget_s, holder):
    cfg = COINS[coin]
    try:
        src = make_source(coin, cfg)
        holder.append(src)
        tip = src.tip()
        tip_time = src.tip_time()
    except (OSError, KeyError, ValueError, FileNotFoundError) as e:
        log.warning("%s: source not available (%s)", coin, type(e).__name__)
        return "unreachable"
    except RPCUnavailable as e:
        log.warning("%s: %s", coin, e)
        return "unreachable"
    if time.time() - tip_time > SYNCED_MAX_AGE_S:
        log.info("%s: node still syncing (tip %d is %.1f h old); not scanning", coin, tip, (time.time() - tip_time) / 3600)
        return "syncing"
    row = db.execute("SELECT last_height, last_hash FROM scan_state WHERE coin=?", (coin,)).fetchone()
    if row:
        # reorg guard: re-check the last blocks we stored against the chain
        for h, bh in db.execute("SELECT height, hash FROM blocks WHERE coin=? ORDER BY height DESC LIMIT ?", (coin, REORG_DEPTH)).fetchall():
            if h <= tip and src.blockhash(h) != bh:
                log.warning("%s: reorg at height %d, rewinding", coin, h)
                rewind(db, coin, h)
        r2 = db.execute("SELECT MAX(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
        start = (r2 + 1) if r2 is not None else None
    else:
        start = None
    # --backfill-days N, or "max" (as deep as the source allows); without it a fresh database starts FIRST_SCAN_DAYS back and an existing one
    # keeps its depth (moves forward, finishes a queued older range, never queues a new one): a forgotten flag cannot start a months-long backfill
    given = args.backfill_days is not None
    days = DEPTH_DAYS.get(coin, 0) if args.backfill_days == "max" else (float(args.backfill_days) if given else FIRST_SCAN_DAYS)
    if start is None and not given:
        log.info("%s: no --backfill-days: first scan covers %.0f days (pass --backfill-days N or max to go further back)", coin, FIRST_SCAN_DAYS)
    if start is None:
        want = max(0, tip - int(cfg["bpd"] * days)) if days else 0
        start = src.earliest(want, tip)
        log.info("%s: first scan starts at height %d (tip %d, %.1f days back)", coin, start, tip, (tip - start) / cfg["bpd"])
    lookup = make_lookup(db, coin)
    thr = Throttle(cfg["rps"] if cfg["mode"] in ("p2p", "part_p2p") else min(args.rps, cfg.get("rps", args.rps)))       # p2p coins: the per-coin cap in COINS (read-ahead peers, not an RPC daemon)
    t_end, done, trips, last_commit = time.time() + budget_s, 0, 0, time.time()
    tm = {"fetch": 0.0, "scan": 0.0, "write": 0.0, "wait": 0.0, "commit": 0.0}      # where the time of this slice goes (logged at its end)
    t_slice = time.time()
    # older range: blocks before the earliest one scanned so far (the header window grew, or --backfill-days was raised); ascending, with its own progress row
    ranges = []
    older = None
    if not args.no_extend_back and not args.dry_run:
        lo_desired = src.earliest(max(0, tip - int(cfg["bpd"] * days)) if days else 0, tip)
        mn = db.execute("SELECT MIN(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
        bf = db.execute("SELECT next_h, end_h FROM backfill WHERE coin=?", (coin,)).fetchone()
        if given and bf is None and mn is not None and lo_desired < mn:
            bf = (lo_desired, mn - 1)
            db.execute("INSERT OR REPLACE INTO backfill VALUES (?,?,?)", (coin, bf[0], bf[1]))
            db.commit()
            log.info("%s: older range %d-%d added to the scan (%.1f days)", coin, bf[0], bf[1], (bf[1] - bf[0]) / cfg["bpd"])
        older = (bf[0], bf[1], True) if (bf is not None and bf[0] <= bf[1]) else None
    ranges.append((start, tip, False))                # the tip first: new blocks must never wait behind a long history backfill
    if older:
        ranges.append(older)
    if cfg["mode"] in ("p2p", "part_p2p") and not args.dry_run:
        try:
            warm_up(db, src, coin, cfg, start, thr, t_end)
        except RPCUnavailable as e:
            log.warning("%s: index warm-up skipped (%s)", coin, e)
    ri = 0
    h, rng_end, in_back = ranges[0][0], ranges[0][1], ranges[0][2]
    while not STOP:
        beat(coin, h)
        if h > rng_end:
            if in_back:
                db.execute("DELETE FROM backfill WHERE coin=?", (coin,))
                db.commit()
            ri += 1
            if ri >= len(ranges):
                break
            h, rng_end, in_back = ranges[ri]
            continue
        if time.time() > t_end:
            break
        if args.max_blocks and done >= args.max_blocks:
            break
        if thr.check(src):
            trips += 1
            log.warning("%s: RPC latency tripwire (median %.2fs), backing off 30 s (%d)", coin, statistics.median(src.lat[-20:]), trips)
            if trips >= 3:
                return "partial"
            time.sleep(30)
            src.lat.clear()
            thr.base = None
            continue
        t0 = time.time()
        thr.wait()
        tm["wait"] += time.time() - t0
        try:
            t0 = time.time()
            bh = src.blockhash(h)
            header, txs = src.block(h, bh)
            tm["fetch"] += time.time() - t0
        except BlockPruned:
            db.execute("INSERT INTO gaps VALUES (?,?,?,?,?)", (coin, h, h, "pruned", int(time.time())))
            h += 1
            continue
        except RPCUnavailable as e:
            log.warning("%s: %s at height %d; stopping this run", coin, e, h)
            db.commit()
            return "partial"
        except (ValueError, KeyError) as e:
            log.error("%s: could not parse block at height %d (%s: %s); skipping, recorded as gap", coin, h, type(e).__name__, str(e)[:80])
            db.execute("INSERT INTO gaps VALUES (?,?,?,?,?)", (coin, h, h, "parse_error", int(time.time())))
            h += 1
            continue
        def write_block():
            # the block's own script-hash outputs go into the index first so same-block spends resolve
            ta = time.time()
            p2, _, _ = L.scan_block(coin, cfg["family"], h, bh, header, txs, lambda a, b: None, wide=False, index_only=True)
            tb = time.time()
            db.executemany("INSERT OR REPLACE INTO p2sh_outputs VALUES (?,?,?,?,?,?,?,?,?)", p2)
            tc = time.time()
            p2, spends, events_ = L.scan_block(coin, cfg["family"], h, bh, header, txs, lookup, wide=not args.no_wide, wide_max_hex=WIDE_MAX_HEX.get(coin, 300))
            td = time.time()
            if args.dry_run:
                db.rollback()
            else:
                insert_block(db, coin, h, bh, header, txs, p2, spends, events_)
                if in_back:
                    db.execute("UPDATE backfill SET next_h=? WHERE coin=?", (h + 1, coin))
                tcm = time.time()
                db.commit()                                  # one transaction per block: the write lock is held only while this block is written, so the three scanner services
                tm["commit"] += time.time() - tcm            # interleave instead of one holding it across its network waits (2026-11-16: the lock convoy was the bottleneck)
            te = time.time()
            tm["scan"] += (tb - ta) + (td - tc)             # parsing and recognition (includes the index lookups of the second pass)
            tm["write"] += (tc - tb) + (te - td)            # index insert and the block's rows
            return events_
        events = db_retry(db, write_block)
        done += 1
        if events:
            log.info("%s: height %d: %s", coin, h, ", ".join("%s/%s" % (e["kind"], e["confidence"]) for e in events))
        # commit every 25 blocks OR every 4 seconds: the two scanner services share one database, and a write transaction held open across slow network fetches
        # starved the other service ("database is locked", found 2026-11-16)
        if (done % 25 == 0 or time.time() - last_commit > 4) and not args.dry_run:
            last_commit = time.time()
            def commit_progress():
                if in_back:
                    db.execute("UPDATE backfill SET next_h=? WHERE coin=?", (h + 1, coin))
                db.commit()
            t0 = time.time()
            db_retry(db, commit_progress)
            tm["commit"] += time.time() - t0
        h += 1
    if in_back and not args.dry_run:
        db_retry(db, lambda: (db.execute("UPDATE backfill SET next_h=? WHERE coin=?", (h, coin)), db.commit()))
    if not args.dry_run:
        db_retry(db, lambda: finalize_run(db, coin, cfg, tip))
    el = max(time.time() - t_slice, 1e-9)
    log.info("%s: scanned %d blocks this run (start %d, tip %d); %.1f blocks/min; time split: fetch %.0f%%, parse+recognise %.0f%%, db write %.0f%%, commit %.0f%%, throttle %.0f%%",
             coin, done, start, tip, done / el * 60, 100 * tm["fetch"] / el, 100 * tm["scan"] / el, 100 * tm["write"] / el, 100 * tm["commit"] / el, 100 * tm["wait"] / el)
    return "caught_up" if (ri >= len(ranges) or (ri == len(ranges) - 1 and h > rng_end)) and not in_back_pending(db, coin) else "partial"


def finalize_run(db, coin, cfg, tip):
    if True:
        last = db.execute("SELECT MAX(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
        if last is not None:
            lh = db.execute("SELECT hash, time FROM blocks WHERE coin=? AND height=?", (coin, last)).fetchone()
            first = db.execute("SELECT MIN(height) FROM blocks WHERE coin=?", (coin,)).fetchone()[0]
            state = "caught_up" if last >= tip else "partial"
            db.execute("INSERT OR REPLACE INTO scan_state VALUES (?,?,?,?,?,?,?)", (coin, last, lh[0], lh[1], first, state, int(time.time())))
        # prune the script-hash index, but never the window just behind either resume point (forward frontier, older-range frontier): a stop and restart
        # in the middle of a long backfill must not lose the locks the next blocks spend
        keep = int(cfg["bpd"] * P2SH_RETAIN_DAYS.get(coin, P2SH_RETAIN_DEFAULT))
        wins = [(last - keep, last)] if last is not None else []
        bf = db.execute("SELECT next_h FROM backfill WHERE coin=?", (coin,)).fetchone()
        if bf:
            wins.append((bf[0] - keep, bf[0]))
        q, qa = "DELETE FROM p2sh_outputs WHERE coin=? AND height < ?", [coin, tip - keep]
        for lo_, hi_ in wins:
            q += " AND NOT (height BETWEEN ? AND ?)"
            qa += [lo_, hi_]
        db.execute(q, qa)
        if coin in SPENDS_RETAIN_DAYS:
            db.execute("DELETE FROM script_spends WHERE coin=? AND time < ?", (coin, int(time.time() - 86400 * SPENDS_RETAIN_DAYS[coin])))
        db.commit()


def coverage(db, coin):
    """completeness of one coin: 'complete to the tip' = every height from the oldest stored block to the newest is stored (no holes), no older range is pending,
    no recorded gap, and the newest stored block is not older than MAX_LAG_H hours (the polling delay)"""
    n, lo, hi, t1 = db.execute("SELECT COUNT(*), MIN(height), MAX(height), MAX(time) FROM blocks WHERE coin=?", (coin,)).fetchone()
    if not n:
        return {"stored": 0, "complete": False}
    t0 = db.execute("SELECT time FROM blocks WHERE coin=? AND height=?", (coin, lo)).fetchone()[0]
    missing = (hi - lo + 1) - n
    older = db.execute("SELECT next_h, end_h FROM backfill WHERE coin=?", (coin,)).fetchone()
    pending = (older[1] - older[0] + 1) if older and older[0] <= older[1] else 0
    gaps = db.execute("SELECT COUNT(*) FROM gaps WHERE coin=?", (coin,)).fetchone()[0]
    lag_h = (time.time() - t1) / 3600
    return {"stored": n, "first_height": lo, "last_height": hi, "days_spanned": round((t1 - t0) / 86400, 1), "missing_inside_range": missing,
            "older_range_blocks_pending": pending, "gaps": gaps, "newest_block_age_h": round(lag_h, 1), "oldest_block_utc": time.strftime("%Y-%m-%d", time.gmtime(t0)),
            "complete": missing == 0 and pending == 0 and gaps == 0 and lag_h <= MAX_LAG_H}


def write_status(db, states):
    st = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "coins": {}}
    for coin in COINS:
        r = db.execute("SELECT last_height, last_time, first_height, state FROM scan_state WHERE coin=?", (coin,)).fetchone()
        ev = dict(db.execute("SELECT kind || '/' || confidence, COUNT(*) FROM swap_events WHERE coin=? GROUP BY 1", (coin,)).fetchall())
        st["coins"][coin] = {"run_state": states.get(coin), "last_height": r[0] if r else None, "first_height": r[2] if r else None,
                             "last_block_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r[1])) if r and r[1] else None, "events": ev,
                             "gaps": db.execute("SELECT COUNT(*) FROM gaps WHERE coin=?", (coin,)).fetchone()[0], "coverage": coverage(db, coin),
                             "retired": RETIRED.get(coin)}
    tmp = STATUS_PATH + ".%d.tmp" % os.getpid()          # three scanner services can finish at the same moment: one temp file each
    try:
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def probe(coins):
    for coin in coins:
        cfg = COINS[coin]
        if cfg["mode"] == "part_p2p":
            try:
                src = PART.PartSource()
                print("%-4s local particld P2P: headers %d-%d, tip block %s UTC (%.1f h old)" % (coin, src.chain.first, src.chain.tip,
                      time.strftime("%Y-%m-%d %H:%M", time.gmtime(src.tip_time())), (time.time() - src.tip_time()) / 3600))
                src.close()
            except (OSError, P2P.P2PError) as e:
                print("%-4s local particld P2P not usable: %s" % (coin, e))
            continue
        if cfg["mode"] == "p2p":
            try:
                src = P2P.P2PSource(coin)
                print("%-4s peer-to-peer source: header window %d-%d (%.1f days), tip block %s UTC (%.1f h old)" % (coin, src.chain.first, src.chain.tip, (src.chain.tip - src.chain.first) / cfg["bpd"],
                      time.strftime("%Y-%m-%d %H:%M", time.gmtime(src.tip_time())), (time.time() - src.tip_time()) / 3600))
            except (OSError, P2P.P2PError) as e:
                print("%-4s header file not usable: %s" % (coin, e))
            continue
        try:
            host, port, user, pw = load_credentials(coin)
            rpc = ChainRPC(coin, host, port, user, pw, timeout=30)
            tip = rpc.call("getblockcount")
            best = rpc.call("getbestblockhash")
            t = header_time(rpc, cfg, best)
            print("%-4s tip %d, tip block %s UTC (%.1f h old), %d ms per call" % (coin, tip, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), (time.time() - t) / 3600, 1000 * statistics.mean(rpc.lat)))
        except (RPCUnavailable, OSError, KeyError, RPCNotAllowed, BlockPruned) as e:
            print("%-4s not reachable: %s" % (coin, type(e).__name__ if not isinstance(e, (RPCUnavailable, RPCNotAllowed)) else e))


def verify(coins, n):
    """parser parity: for the last n blocks compare our raw/JSON parse with the node's own txid list (getblock verbosity 1);
    peer-to-peer coins: fetch the last n blocks and require header proof of work and merkle root to match"""
    for coin in coins:
        cfg = COINS[coin]
        if cfg["mode"] == "part_p2p":
            try:
                src = PART.PartSource()
                tip, ntx, nsp = src.tip(), 0, 0
                for h in range(tip - n, tip + 1):
                    head, txs = src.block(h)
                    ntx += len(txs)
                    nsp += len(L.scan_block(coin, cfg["family"], h, src.blockhash(h), head, txs, lambda a, b: None, wide=True)[1])
                print("%-4s verified %d blocks from the local particld, %d txs: block hash and merkle root match; script-revealing spends: %d" % (coin, n + 1, ntx, nsp))
            except (OSError, P2P.P2PError, ValueError) as e:
                print("%-4s PART verify failed: %s" % (coin, e))
            continue
        if cfg["mode"] == "p2p":
            try:
                src = P2P.P2PSource(coin)
                tip, ntx, nsp = src.tip(), 0, 0
                for h in range(tip - n - 1, tip - 1):
                    head, txs = src.block(h)
                    ntx += len(txs)
                    nsp += len(L.scan_block(coin, cfg["family"], h, src.blockhash(h), head, txs, lambda a, b: None, wide=True)[1])
                print("%-4s verified %d blocks from the network, %d txs: proof of work and merkle root match the headers; script-revealing spends: %d" % (coin, n, ntx, nsp))
            except (OSError, P2P.P2PError) as e:
                print("%-4s p2p verify failed: %s" % (coin, e))
            continue
        try:
            host, port, user, pw = load_credentials(coin)
            rpc = ChainRPC(coin, host, port, user, pw, timeout=120)
            tip = rpc.call("getblockcount")
            ok = bad = ntx = nscript = 0
            for h in range(tip - n + 1, tip + 1):
                bh = rpc.call("getblockhash", [h])
                head, txs = fetch_block(rpc, cfg, bh)
                ref = rpc.call("getblock", [bh, True if cfg["mode"] == "dcr" else 1])
                ref_tx = [t["txid"] if isinstance(t, dict) else t for t in ref["tx"]]
                mine = [t["txid"] for t in txs]
                same = (mine == ref_tx) and head["time"] == int(ref["time"])
                ok += same
                bad += not same
                ntx += len(txs)
                p2, sp, ev = L.scan_block(coin, cfg["family"], h, bh, head, txs, lambda a, b: None, wide=True)
                nscript += len(sp)
                time.sleep(0.3)
            print("%-4s verified %d blocks, %d txs: %d identical, %d different; script-revealing spends in them: %d" % (coin, n, ntx, ok, bad, nscript))
        except (RPCUnavailable, OSError, KeyError, RPCNotAllowed, BlockPruned, ValueError) as e:
            print("%-4s verify failed: %s %s" % (coin, type(e).__name__, str(e)[:100]))


def main():
    global log
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default=DEFAULT_COINS)
    ap.add_argument("--config", help="settings file (paths, protected ports); see collector_settings.py")
    ap.add_argument("--probe", action="store_true", help="connectivity test only (getblockcount, getbestblockhash, one header)")
    ap.add_argument("--coverage", action="store_true", help="print how complete the stored history is per coin (holes, pending older range, lag to the tip) and exit")
    ap.add_argument("--earliest", action="store_true", help="print the oldest block each node still has (bisection with header-level calls)")
    ap.add_argument("--extend-headers", action="store_true", help="BTC/LTC: walk the header chain from genesis (about 78 MB / 257 MB in transit, only the last --keep-days days are kept) so older blocks can be scanned")
    ap.add_argument("--keep-days", type=float, default=150.0)
    ap.add_argument("--verify", type=int, default=0, help="parser parity check on the last N blocks (txid lists must equal the node's own)")
    ap.add_argument("--backfill-days", default=None, help="how many days back to scan every coin, or max (as deep as each source allows); without it a new database starts %g days back and an existing one keeps its depth" % FIRST_SCAN_DAYS)
    ap.add_argument("--rps", type=float, default=3.0, help="max RPC blocks per second per coin")
    ap.add_argument("--max-seconds", type=int, default=3300, help="total time budget of one run")
    ap.add_argument("--slice-seconds", type=int, default=600, help="time slice per coin per round (round robin)")
    ap.add_argument("--max-blocks", type=int, default=0)
    ap.add_argument("--no-extend-back", action="store_true", help="do not scan older ranges that became available (header window grown, longer --backfill-days)")
    ap.add_argument("--no-wide", action="store_true", help="do not keep the wide script_spends table")
    ap.add_argument("--dry-run", action="store_true", help="parse and recognise but write nothing")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--lock-name", default="chain_swap_scan", help="single-instance lock name; the peer-to-peer coins run as a second instance with their own lock")
    a = ap.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    if any(c not in COINS for c in coins):
        sys.exit("unknown coin; known: %s" % ",".join(COINS))
    if a.extend_headers:
        log = setup_logging("chain_swap_scan")
        for coin in coins:
            if COINS[coin]["mode"] != "p2p":
                continue
            try:
                n = P2P.extend_headers(coin, a.keep_days, log=log)
                ext = P2P.ExtendedHeaders(coin, os.path.join(OUT_DIR, "headers_%s.bin" % coin))
                print("%-4s headers: %s; usable window now heights %d-%d (%.1f days)" % (coin, "walked and kept %d" % n if n else "already extended", ext.first, ext.tip, (ext.tip - ext.first) / COINS[coin]["bpd"]))
            except (OSError, P2P.P2PError) as e:
                print("%-4s header extension failed: %s" % (coin, e))
        return
    if a.coverage:
        db = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
        print("%-5s %8s %7s %-10s %8s %10s %5s %9s  %s" % ("coin", "stored", "days", "from", "holes", "older left", "gaps", "age(h)", "complete to the tip"))
        for coin in (coins if a.coins != DEFAULT_COINS else list(COINS)):          # no --coins given: every coin, retired ones marked
            c = coverage(db, coin)
            if not c["stored"]:
                print("%-5s nothing stored" % coin)
                continue
            if coin in RETIRED:
                print("%-5s %8d %7.1f %-10s retired from the scan 2026-11-16 (%s); stored history kept, frozen at %.0f h ago" % (coin, c["stored"], c["days_spanned"], c["oldest_block_utc"], RETIRED[coin], c["newest_block_age_h"]))
                continue
            print("%-5s %8d %7.1f %-10s %8d %10d %5d %9.1f  %s" % (coin, c["stored"], c["days_spanned"], c["oldest_block_utc"], c["missing_inside_range"], c["older_range_blocks_pending"], c["gaps"], c["newest_block_age_h"], "YES" if c["complete"] else "no"))
        return
    if a.probe:
        probe(coins)
        return
    if a.verify:
        verify(coins, a.verify)
        return
    if a.earliest:
        for coin in coins:
            cfg = COINS[coin]
            if cfg["mode"] == "part_p2p":
                src = PART.PartSource()
                print("%-4s oldest stored header %d of %d (%.1f days back)" % (coin, src.chain.first, src.chain.tip, (src.chain.tip - src.chain.first) / cfg["bpd"]))
                continue
            if cfg["mode"] == "p2p":
                src = P2P.P2PSource(coin)
                print("%-4s oldest usable header %d of %d (%.1f days back; older headers are not in the BasicSwap header file)" % (coin, src.chain.first + 1, src.chain.tip, (src.chain.tip - src.chain.first) / cfg["bpd"]))
                continue
            try:
                host, port, user, pw = load_credentials(coin)
                rpc = ChainRPC(coin, host, port, user, pw, timeout=60)
                tip = rpc.call("getblockcount")
                lo = earliest_available(rpc, cfg, 0, tip)
                t = header_time(rpc, cfg, rpc.call("getblockhash", [lo]))
                print("%-4s oldest readable block %d of %d, dated %s UTC (%.1f days back)" % (coin, lo, tip, time.strftime("%Y-%m-%d", time.gmtime(t)), (time.time() - t) / 86400))
            except (RPCUnavailable, OSError, KeyError, RPCNotAllowed, BlockPruned) as e:
                print("%-4s earliest failed: %s" % (coin, e))
        return
    if not a.no_lock:
        single_instance(a.lock_name)
    log = setup_logging("chain_swap_scan")
    global HEARTBEAT
    HEARTBEAT = os.path.join(OUT_DIR, "heartbeat_%s.json" % a.lock_name)
    signal.signal(signal.SIGTERM, _sigterm)
    db = open_db(a.db)
    beat(note="start")
    states, t_end = {}, time.time() + a.max_seconds
    while time.time() < t_end and not STOP:
        progressed = False
        for coin in coins:
            if states.get(coin) in ("caught_up", "syncing", "unreachable", "error") and states.get(coin) != "partial":
                continue
            states[coin] = scan_coin(db, coin, a, min(a.slice_seconds, max(10, t_end - time.time())))
            progressed = progressed or states[coin] == "partial"
            if time.time() >= t_end or STOP:
                break
        if not progressed or a.max_blocks or a.dry_run:      # a capped or dry run is one pass over the coins, not a loop until the time budget
            break
    write_status(db, states)
    db.close()
    log.info("run finished: %s", states)


if __name__ == "__main__":
    main()
