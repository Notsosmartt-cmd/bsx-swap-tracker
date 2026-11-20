"""Fetch SELECTED recent blocks of Bitcoin and Litecoin straight from the peer-to-peer network, with no node, no sync and no third party.

Why (docs/architecture.md): a pruned node still downloads and verifies the whole chain once (BTC about 770 GB). The swap scan only needs
the last weeks of blocks, so this module asks archival peers for exactly those blocks.
  * Block hashes come from the header chains that the BasicSwap node already keeps for its Electrum-mode BTC and LTC clients
    (coindata/electrum_headers/<coin>/mainnet/blockchain_headers, opened read-only, 80 bytes per header, only the recent real part is filled in).
  * Authenticity: the usable window must be a contiguous, correctly linked header run; the header of every block we use must satisfy its own proof of
    work (sha256d for BTC, scrypt for LTC); the block we receive must hash to the requested header and its transactions must hash to the header's merkle root.
  * Read-only on the network side too: the handshake announces no services, asks for no transactions (relay=false), serves nothing, sends nothing else.
It is NOT a validating node: it does not check scripts or signatures. That is fine for reading public swap footprints, and it must never be used to decide
anything about funds.
"""
import hashlib
import mmap
import os
import random
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector_common import ROOT  # noqa: E402
import chainscan_lib as L  # noqa: E402
from collector_settings import S as SETTINGS  # noqa: E402

NETS = {
    "BTC": dict(magic=bytes.fromhex("f9beb4d9"), port=8333, family="btc", per_day=144, scrypt=False,
                workers=4, batch=6, max_bytes=60 << 20,
                seeds=["dnsseed.emzy.de", "seed.btc.petertodd.net", "seed.mainnet.achownodes.xyz"],
                headers="bitcoin/mainnet/blockchain_headers",          # under SETTINGS.electrum_headers_dir
                genesis="000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"),
    "LTC": dict(magic=bytes.fromhex("fbc0b6db"), port=9333, family="ltc", per_day=576, scrypt=True,
                workers=4, batch=24, max_bytes=24 << 20,
                seeds=["dnsseed.koin-project.com", "dnsseed.litecointools.com", "dnsseed.ltcpool.org", "dnsseed.thrasher.io", "seed-a.litecoin.loshan.co.uk", "seed-b.litecoin.loshan.co.uk"],
                headers="litecoin/mainnet/blockchain_headers",
                genesis="12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2"),
}
NODE_NETWORK, NODE_WITNESS = 1, 8
MSG_BLOCK_WITNESS = 0x40000002
PROTOCOL = 70015
ZERO80 = b"\x00" * 80


class P2PError(Exception):
    pass


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def bits_target(bits):
    return (bits & 0xFFFFFF) * (1 << (8 * ((bits >> 24) - 3)))


def pow_ok(header, scrypt):
    h = hashlib.scrypt(header, salt=header, n=1024, r=1, p=1, dklen=32) if scrypt else dsha(header)
    return int.from_bytes(h, "little") <= bits_target(struct.unpack("<I", header[72:76])[0])


def merkle_root(txids_hex):
    layer = [bytes.fromhex(t)[::-1] for t in txids_hex]
    if not layer:
        return b"\x00" * 32
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


class HeaderChain:
    """read-only view of the Electrum client's header file: the contiguous, linked, non-empty run that ends at the tip"""

    def __init__(self, path):
        self.path = path
        self.refresh()

    def refresh(self):
        with open(self.path, "rb") as f:
            self.size = os.fstat(f.fileno()).st_size
            self.buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        self.n = self.size // 80
        i = self.n - 1
        while i > 0:                                       # walk back while the run is real and linked
            cur, prev = self.buf[i * 80:(i + 1) * 80], self.buf[(i - 1) * 80:i * 80]
            if cur == ZERO80 or prev == ZERO80 or cur[4:36] != dsha(prev):
                break
            i -= 1
        self.first = i                                     # lowest height of the linked run
        self.tip = self.n - 1

    def header(self, h):
        if not self.first <= h <= self.tip:
            raise P2PError("height %d outside the verified header window %d-%d" % (h, self.first, self.tip))
        return self.buf[h * 80:(h + 1) * 80]

    def hash(self, h):
        return dsha(self.header(h))

    def time(self, h):
        return struct.unpack("<I", self.header(h)[68:72])[0]


class ExtendedHeaders:
    """Header chain = our own cache of OLDER headers (heights keep_from .. first-1, written by extend_headers) joined to the Electrum client's live file.
    Authenticity of the older part: it was walked forward from the genesis block with every prev-hash link checked and it hash-joins the real header at
    `first`; forging an older header that hashes to a real header's prev field is not possible, so the cache can only hold the real chain."""

    def __init__(self, coin, cache_path):
        self.coin, self.cache_path = coin, cache_path
        self.live = HeaderChain(os.path.join(SETTINGS.electrum_headers_dir, NETS[coin]["headers"]))
        self.cache_start, self.cache = None, b""
        self.refresh()

    def refresh(self):
        self.live.refresh()
        self.cache_start, self.cache = None, b""
        try:
            with open(self.cache_path, "rb") as f:
                start = struct.unpack("<I", f.read(4))[0]
                data = f.read()
            n = len(data) // 80
            # still joined to the live file?  (the live file's first real header must link to the cache's last header)
            if n and start + n == self.live.first and self.live.header(self.live.first)[4:36] == dsha(data[(n - 1) * 80:n * 80]):
                self.cache_start, self.cache = start, data
        except (OSError, struct.error, P2PError):
            pass
        self.first = self.cache_start if self.cache_start is not None else self.live.first
        self.tip = self.live.tip

    def header(self, h):
        if h >= self.live.first:
            return self.live.header(h)
        if self.cache_start is not None and self.cache_start <= h < self.live.first:
            i = h - self.cache_start
            return self.cache[i * 80:(i + 1) * 80]
        raise P2PError("height %d outside the verified header window %d-%d" % (h, self.first, self.tip))

    def hash(self, h):
        return dsha(self.header(h))

    def time(self, h):
        return struct.unpack("<I", self.header(h)[68:72])[0]


def getheaders_payload(locator_hashes):
    return struct.pack("<I", PROTOCOL) + bytes([len(locator_hashes)]) + b"".join(locator_hashes) + b"\x00" * 32


def extend_headers(coin, keep_days=150, log=None, max_seconds=1800):
    """Walk the header chain from genesis through one archival peer (80 bytes a header, about 78 MB for BTC and 257 MB for LTC in transit, nothing kept except the
    last `keep_days` days), check every prev-hash link, require the walk to join the live Electrum file's first real header, and write the cache.
    Returns the number of headers kept."""
    net = NETS[coin]
    live = HeaderChain(os.path.join(SETTINGS.electrum_headers_dir, net["headers"]))
    F = live.first
    keep_from = max(1, F - int(keep_days * net["per_day"]))
    cache_path = os.path.join(SETTINGS.data_dir, "headers_%s.bin" % coin)
    ext = ExtendedHeaders(coin, cache_path)
    if ext.cache_start is not None and ext.cache_start <= keep_from + 5:
        return 0
    want_prev = live.header(F)[4:36]
    last = bytes.fromhex(net["genesis"])[::-1]
    height, kept = 0, []
    peer = None
    t_end = time.time() + max_seconds
    names = list(net["seeds"])
    reconnects = 0

    def connect():
        for _ in range(40):
            name = random.choice(names)
            try:
                ip = random.choice([a[4][0] for a in socket.getaddrinfo(name, net["port"], socket.AF_INET, socket.SOCK_STREAM)])
                p = Peer(coin, ip, timeout=20)
                p.handshake()
                return p
            except (OSError, P2PError):
                continue
        raise P2PError("no peer for the header walk")

    try:
        peer = connect()
        while height < F - 1 and time.time() < t_end:
            try:
                peer.send("getheaders", getheaders_payload([last]))
                while True:
                    cmd, payload = peer.recv()
                    if cmd == "headers":
                        break
                    if cmd == "ping":
                        peer.send("pong", payload)
            except (OSError, P2PError):                                # the peer dropped us (rate limit, restart): carry on from the last header with another peer
                reconnects += 1
                if reconnects > 60:
                    raise P2PError("too many peer drops during the header walk")
                peer.close()
                peer = connect()
                continue
            r = L.Reader(payload)
            n = r.varint()
            if n == 0:
                raise P2PError("peer sent no headers at height %d" % height)
            for _ in range(n):
                hd = r.read(80)
                r.varint()                                         # transaction count (always 0)
                if hd[4:36] != last:
                    raise P2PError("header chain does not link at height %d" % (height + 1))
                last = dsha(hd)
                height += 1
                if height >= keep_from and height <= F - 1:
                    kept.append(hd)
                if height >= F - 1:
                    break
            if log and height % 100000 < 2000:
                log.info("%s: header walk at height %d of %d (%d peer drops so far)", coin, height, F - 1, reconnects)
        if height != F - 1 or last != want_prev:
            raise P2PError("header walk did not join the live header file (height %d, join %s)" % (height, last == want_prev))
    finally:
        if peer:
            peer.close()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = cache_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<I", keep_from) + b"".join(kept))
    os.replace(tmp, cache_path)
    return len(kept)


# ---------------------------------------------------------------- wire protocol


def pack_msg(magic, cmd, payload):
    return magic + cmd.encode().ljust(12, b"\0") + struct.pack("<I", len(payload)) + dsha(payload)[:4] + payload


def varstr(s):
    b = s.encode()
    return bytes([len(b)]) + b


def version_payload(nonce=None, start_height=0):
    na = struct.pack("<Q", 0) + b"\0" * 10 + b"\xff\xff" + b"\0\0\0\0" + struct.pack(">H", 0)
    return (struct.pack("<iQq", PROTOCOL, 0, int(time.time())) + na + na + struct.pack("<Q", nonce or random.getrandbits(64))
            + varstr("/chainscan:0.1/") + struct.pack("<i", start_height) + b"\0")


def parse_version(payload):
    ver, services = struct.unpack("<iQ", payload[:12])
    return ver, services


class Peer:
    def __init__(self, coin, host, timeout=20):
        self.net, self.coin, self.host, self.timeout = NETS[coin], coin, host, timeout
        self.sock = socket.create_connection((host, self.net["port"]), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.bytes_in = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, cmd, payload=b""):
        self.sock.sendall(pack_msg(self.net["magic"], cmd, payload))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise P2PError("peer closed the connection")
            self.buf += chunk
            self.bytes_in += len(chunk)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        hdr = self._read(24)
        if hdr[:4] != self.net["magic"]:
            raise P2PError("wrong network magic")
        cmd = hdr[4:16].rstrip(b"\0").decode("ascii", "replace")
        ln = struct.unpack("<I", hdr[16:20])[0]
        if ln > 64 * 1024 * 1024:
            raise P2PError("oversized message")
        payload = self._read(ln)
        if dsha(payload)[:4] != hdr[20:24]:
            raise P2PError("bad checksum")
        return cmd, payload

    def handshake(self):
        self.send("version", version_payload())
        got_version = got_verack = False
        end = time.time() + self.timeout
        while not (got_version and got_verack) and time.time() < end:
            cmd, payload = self.recv()
            if cmd == "version":
                ver, services = parse_version(payload)
                if not (services & NODE_NETWORK and services & NODE_WITNESS):
                    raise P2PError("peer is not an archival witness node (services %#x)" % services)
                got_version = True
                self.send("verack")
            elif cmd == "verack":
                got_verack = True
            elif cmd == "ping":
                self.send("pong", payload)
        if not (got_version and got_verack):
            raise P2PError("handshake timed out")

    def get_blocks(self, hashes):
        """request blocks (raw hash bytes, internal byte order); -> {hash: raw block bytes}; missing ones are simply absent"""
        inv = b"".join(struct.pack("<I", MSG_BLOCK_WITNESS) + h for h in hashes)
        self.send("getdata", bytes([len(hashes)]) + inv if len(hashes) < 0xFD else b"\xfd" + struct.pack("<H", len(hashes)) + inv)
        want, out, end = set(hashes), {}, time.time() + self.timeout * 3
        while want and time.time() < end:
            cmd, payload = self.recv()
            if cmd == "block":
                h = dsha(payload[:80])
                if h in want:
                    out[h] = payload
                    want.discard(h)
            elif cmd == "notfound":
                break
            elif cmd == "ping":
                self.send("pong", payload)
        return out


class BlockPrefetcher:
    """Read-ahead of raw blocks from several archival peers at once (2026-11-16).

    The scan asks for heights in ascending order; this keeps a few worker threads, each with its OWN peer (a different IP), fetching the next heights in batches
    (one getdata for several blocks, so a block does not cost a round trip) while the consumer parses and stores the previous ones.  Reading only: the same
    handshake and the same getdata as before, just more of them in flight.  A block is accepted only if its header hashes to the hash we asked for (get_blocks);
    the caller still checks proof of work and the merkle root.  Failures: a peer that errors or lacks blocks is dropped and its heights are asked again from
    another peer; a worker that cannot find a peer waits and tries again; the consumer gets a P2PError after `timeout` seconds without the block it needs.
    Memory is bounded by max_bytes of undelivered raw blocks (the block the consumer waits for is always allowed).
    connect() -> object with get_blocks(list of raw hashes) -> {hash: raw} and close();  hash_of(h) -> raw hash bytes;  tip_of() -> highest height we may ask for."""

    def __init__(self, connect, hash_of, tip_of, workers=4, batch=8, max_bytes=48 << 20, window=4096, log=None, name="", pause=lambda n: min(30.0, 1.0 * n)):
        self.connect, self.hash_of, self.tip_of = connect, hash_of, tip_of
        self.workers, self.batch, self.max_bytes, self.window, self.log, self.name, self.pause = workers, batch, max_bytes, window, log, name, pause
        self.cv = threading.Condition()
        self.results, self.inflight = {}, set()
        self.base, self.nbytes, self.stop = None, 0, False
        self.avg = 0.0                                                       # running mean size of a delivered block, to count what is in flight
        self.threads = []
        self.stats = {"batches": 0, "blocks": 0, "peer_errors": 0, "missing": 0, "waits": 0.0}

    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, args=(i,), name="prefetch-%s-%d" % (self.name, i), daemon=True)
            t.start()
            self.threads.append(t)

    def close(self):
        with self.cv:
            self.stop = True
            self.cv.notify_all()
        for t in self.threads:
            t.join(2)

    def _next_batch(self):
        """caller holds the lock: the next run of heights not yet fetched or in flight, starting at the consumer's position"""
        if self.base is None:
            return []
        tip, out = self.tip_of(), []
        h = self.base
        while h <= tip and h < self.base + self.window and len(out) < self.batch:
            if h not in self.results and h not in self.inflight:
                if self.nbytes + len(self.inflight) * self.avg >= self.max_bytes and h != self.base:
                    break
                out.append(h)
            elif out:
                break                                                        # keep a batch contiguous
            h += 1
        return out

    def _worker(self, idx):
        peer, errs = None, 0
        while True:
            with self.cv:
                batch = []
                while not self.stop:
                    batch = self._next_batch()
                    if batch:
                        break
                    self.cv.wait(0.5)
                if self.stop:
                    if peer:
                        peer.close()
                    return
                self.inflight.update(batch)
            got, hashes = {}, []
            try:
                if peer is None:
                    peer = self.connect()
                hashes = [self.hash_of(h) for h in batch]
                got = peer.get_blocks(hashes)
                errs = 0
            except Exception:                                                  # peer trouble of any kind: drop this peer, ask again elsewhere
                errs += 1
                with self.cv:
                    self.stats["peer_errors"] += 1
                if peer:
                    peer.close()
                peer = None
            with self.cv:
                if not got and peer is not None:                               # a peer that answers none of a batch does not have these blocks
                    self.stats["missing"] += len(batch)
                    peer.close()
                    peer = None
                for h, hs in zip(batch, hashes):
                    self.inflight.discard(h)
                    raw = got.get(hs)
                    if raw is not None and self.base is not None and h >= self.base and h not in self.results:
                        self.results[h] = raw
                        self.nbytes += len(raw)
                        self.stats["blocks"] += 1
                        self.avg += (len(raw) - self.avg) / min(self.stats["blocks"], 50)
                if not hashes:
                    for h in batch:
                        self.inflight.discard(h)
                self.stats["batches"] += 1
                self.cv.notify_all()
            if peer is None:
                time.sleep(self.pause(errs) if errs else 0.2)

    def get(self, h, timeout=90.0):
        """raw bytes of the block at height h (blocks until a worker delivers it)"""
        t0 = time.time()
        with self.cv:
            if self.base is None or h < self.base or h > self.base + self.window:
                for k in list(self.results):                               # a jump: what was read ahead is of no use
                    self.nbytes -= len(self.results.pop(k))
            for k in [k for k in self.results if k < h]:
                self.nbytes -= len(self.results.pop(k))
            self.base = h
            self.cv.notify_all()
            end = time.time() + timeout
            while h not in self.results:
                if self.stop or time.time() > end:
                    raise P2PError("no peer delivered block %d within %.0f s" % (h, timeout))
                self.cv.wait(0.5)
            raw = self.results.pop(h)
            self.nbytes -= len(raw)
            self.stats["waits"] += time.time() - t0
            self.cv.notify_all()
            return raw


class _TrackedPeer:
    """a Peer that frees its IP for reuse when closed"""

    def __init__(self, src, peer, ip):
        self.src, self.peer, self.ip = src, peer, ip

    def get_blocks(self, hashes):
        return self.peer.get_blocks(hashes)

    def close(self):
        self.peer.close()
        with self.src.lock:
            self.src.in_use.discard(self.ip)


class P2PSource:
    """block source for chain_swap_scan: tip, tip time, block hash by height, and verified blocks"""

    def __init__(self, coin, log=None, workers=None):
        self.coin, self.net, self.log = coin, NETS[coin], log
        self.chain = ExtendedHeaders(coin, os.path.join(SETTINGS.data_dir, "headers_%s.bin" % coin))
        self.peer = None
        self.lat = []
        self.bad_peers = set()
        self.in_use = set()
        self.lock = threading.Lock()
        self.pf = None
        self.workers = self.net.get("workers", 4) if workers is None else workers

    # --- same shape as the RPC source
    def tip(self):
        self.chain.refresh()
        return self.chain.tip

    def tip_time(self):
        return self.chain.time(self.chain.tip)

    def earliest(self, want, tip):
        return max(want, self.chain.first + 1)

    def blockhash(self, h):
        return self.chain.hash(h)[::-1].hex()

    def _connect(self):
        if self.peer:
            return
        names = list(self.net["seeds"])
        random.shuffle(names)
        tried = 0
        for name in names:
            try:
                addrs = list({a[4][0] for a in socket.getaddrinfo(name, self.net["port"], socket.AF_INET, socket.SOCK_STREAM)})
            except OSError:
                continue
            random.shuffle(addrs)
            for ip in addrs[:12]:
                if ip in self.bad_peers:
                    continue
                tried += 1
                p = None
                try:
                    p = Peer(self.coin, ip, timeout=15)
                    p.handshake()
                    self.peer = p
                    if self.log:
                        self.log.info("%s: connected to archival peer %s", self.coin, ip)
                    return
                except (OSError, P2PError):
                    self.bad_peers.add(ip)
                    if p:
                        p.close()
                    if tried >= 60:
                        break
        raise P2PError("no archival peer found")

    def _new_peer(self):
        """a handshaken archival peer on an IP no other worker uses (thread-safe); P2PError when none is found"""
        names = list(self.net["seeds"])
        random.shuffle(names)
        tried = 0
        for name in names:
            try:
                addrs = list({a[4][0] for a in socket.getaddrinfo(name, self.net["port"], socket.AF_INET, socket.SOCK_STREAM)})
            except OSError:
                continue
            random.shuffle(addrs)
            for ip in addrs[:12]:
                with self.lock:
                    if ip in self.bad_peers or ip in self.in_use:
                        continue
                    self.in_use.add(ip)
                tried += 1
                p = None
                try:
                    p = Peer(self.coin, ip, timeout=15)
                    p.handshake()
                    if self.log:
                        self.log.info("%s: connected to archival peer %s", self.coin, ip)
                    return _TrackedPeer(self, p, ip)
                except (OSError, P2PError):
                    with self.lock:
                        self.bad_peers.add(ip)
                        self.in_use.discard(ip)
                    if p:
                        p.close()
                    if tried >= 40:
                        break
        raise P2PError("no archival peer found")

    def _prefetcher(self):
        if self.pf is None:
            self.pf = BlockPrefetcher(self._new_peer, lambda h: self.chain.hash(h), lambda: self.chain.tip, workers=self.workers,
                                      batch=self.net.get("batch", 8), max_bytes=self.net.get("max_bytes", 48 << 20), log=self.log, name=self.coin)
            self.pf.start()
        return self.pf

    def close(self):
        if self.pf:
            self.pf.close()
            self.pf = None
        if self.peer:
            self.peer.close()
            self.peer = None

    def _fetch_single(self, want, h):
        raw = None
        for attempt in range(4):
            try:
                self._connect()
                got = self.peer.get_blocks([want])
                if want in got:
                    raw = got[want]
                    break
                raise P2PError("peer does not have the block")
            except (OSError, P2PError):
                if self.peer:
                    self.bad_peers.add(self.peer.host)
                    self.peer.close()
                    self.peer = None
        if raw is None:
            raise P2PError("could not fetch block %d" % h)
        return raw

    def block(self, h, bh_hex=None):
        """-> (header dict, normalised txs) for the block at height h, fully checked against the header"""
        hdr = self.chain.header(h)
        want = self.chain.hash(h)
        if not pow_ok(hdr, self.net["scrypt"]):
            raise P2PError("header at %d fails its own proof of work" % h)
        t0 = time.time()
        if self.workers > 0:
            raw = self._prefetcher().get(h)                              # several peers read ahead; see BlockPrefetcher
            if dsha(raw[:80]) != want:
                raise P2PError("peer sent a different block than the header at %d" % h)
        else:
            raw = self._fetch_single(want, h)                            # one peer, one block at a time (workers=0)
        header, txs = L.parse_raw_block(raw, self.net["family"])
        if merkle_root([t["txid"] for t in txs]) != hdr[36:68]:
            raise P2PError("merkle root mismatch at height %d" % h)
        self.lat.append(time.time() - t0)
        del self.lat[:-50]
        return header, txs
