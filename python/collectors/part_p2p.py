"""Particl (PART) blocks for the chain swap scan and the chain watch, from the LOCAL particld's peer-to-peer port (127.0.0.1:51738), 2026-11-19.

Why this path: BasicSwap puts its swap lock (a P2WSH 2-of-2, as on BTC) on PART for PART offers, so PART swaps are visible on chain.  The
particld RPC port (19792) is a protected mainnet-wallet port that the scan refuses by design (chain_swap_scan.PROTECTED_PORTS); the P2P protocol
has no wallet access at all, and the local node has already validated every block it serves (Particl is proof of stake, so a header cannot be
checked by its own work as BTC and LTC are).
  * Only 127.0.0.1 is ever contacted.  The handshake announces no services and serves nothing; relay is off unless the chain watch asks for it.
  * Headers: synced from genesis on first use (2,000 per request, about 10 s for 2.3 million), linked hash by hash (block hash = sha256d of the
    112-byte Particl header), the newest KEEP_DAYS kept in <data_dir>/headers_PART.bin (112 bytes each) with headers_PART.json
    (first height).  Later runs ask only for headers after a locator of known hashes; a reorganisation is followed back to the fork point.
  * Blocks: the block must hash to the requested header and its transactions' txids must hash to the header's merkle root (which also proves the
    Particl transaction parser in chainscan_lib.parse_part_tx read every byte right).
Format facts verified against the live node on 2026-11-19: protocol 90035 accepted (90009 refused), header entries of 113 bytes in `headers`
(112 + empty tx count), genesis 0000ee0784c195317ac95623e22fddb8c7b8825dc3998e0bb924d66866eccf4c.
"""
import json, os, random, socket, struct, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector_common import ROOT  # noqa: E402
import chainscan_lib as L  # noqa: E402
from p2p_blocks import P2PError, dsha, merkle_root  # noqa: E402
from collector_settings import S as SETTINGS  # noqa: E402

NET = dict(magic=b"\xfb\xf2\xef\xb4", host="127.0.0.1", port=51738, family="part", protocol=90035, per_day=720, hdr_len=112)
GENESIS = bytes.fromhex("0000ee0784c195317ac95623e22fddb8c7b8825dc3998e0bb924d66866eccf4c")[::-1]
MSG_BLOCK, MSG_TX, MSG_WITNESS_BLOCK = 2, 1, 0x40000002   # witness blocks: a plain block arrives with every witness stripped (the swap scripts live there)
KEEP_DAYS = 400
STORE = os.path.join(SETTINGS.data_dir, "headers_PART.bin")


def version_payload(relay=False):
    na = struct.pack("<Q", 0) + b"\0" * 10 + b"\xff\xff" + b"\0\0\0\0" + struct.pack(">H", 0)
    ua = b"/chainscan:0.2/"
    return (struct.pack("<iQq", NET["protocol"], 0, int(time.time())) + na + na + struct.pack("<Q", random.getrandbits(64))
            + bytes([len(ua)]) + ua + struct.pack("<i", 0) + (b"\x01" if relay else b"\x00"))


class LocalPeer:
    """One P2P connection to the local particld.  recv() keeps partial messages buffered across timeouts."""

    def __init__(self, relay=False, timeout=30):
        if NET["host"] != "127.0.0.1":
            raise P2PError("PART source talks to 127.0.0.1 only")
        self.sock = socket.create_connection((NET["host"], NET["port"]), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf, self.timeout, self.start_height = b"", timeout, None
        self.send("version", version_payload(relay))
        got_v = got_ack = False
        end = time.time() + timeout
        while not (got_v and got_ack):
            cmd, p = self.recv(end - time.time())
            if cmd is None:
                raise P2PError("PART handshake timed out")
            if cmd == "version":
                self.start_height = struct.unpack("<i", p[-5:-1])[0] if len(p) >= 85 else None
                got_v = True
                self.send("verack")
            elif cmd == "verack":
                got_ack = True

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, cmd, payload=b""):
        self.sock.sendall(NET["magic"] + cmd.encode().ljust(12, b"\0") + struct.pack("<I", len(payload)) + dsha(payload)[:4] + payload)

    def recv(self, timeout):
        end = time.time() + max(0.1, timeout)
        while True:
            if len(self.buf) >= 24:
                if self.buf[:4] != NET["magic"]:
                    raise P2PError("wrong network magic")
                ln = struct.unpack("<I", self.buf[16:20])[0]
                if ln > 64 * 1024 * 1024:
                    raise P2PError("oversized message")
                if len(self.buf) >= 24 + ln:
                    p = self.buf[24:24 + ln]
                    if dsha(p)[:4] != self.buf[20:24]:
                        raise P2PError("bad checksum")
                    cmd = self.buf[4:16].rstrip(b"\0").decode("ascii", "replace")
                    self.buf = self.buf[24 + ln:]
                    if cmd == "ping":
                        self.send("pong", p)
                        continue
                    return cmd, p
            left = end - time.time()
            if left <= 0:
                return None, None
            self.sock.settimeout(min(left, 15))
            try:
                c = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            if not c:
                raise P2PError("particld closed the connection")
            self.buf += c

    def wait_for(self, want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            cmd, p = self.recv(end - time.time())
            if cmd == want:
                return p
            if cmd == "notfound" and want == "block":
                raise P2PError("particld does not have the block")
        raise P2PError("no %s reply within %d s" % (want, timeout))

    def getheaders(self, locator):
        self.send("getheaders", struct.pack("<I", NET["protocol"]) + L._compact(len(locator)) + b"".join(locator) + b"\0" * 32)
        p = self.wait_for("headers", self.timeout)
        r = L.Reader(p)
        n = r.varint()
        out = []
        for _ in range(n):
            out.append(r.read(112))
            r.varint()                                         # empty tx count
        return out

    def getblock(self, h):
        self.send("getdata", b"\x01" + struct.pack("<I", MSG_WITNESS_BLOCK) + h)
        end = time.time() + self.timeout * 2
        while time.time() < end:
            p = self.wait_for("block", end - time.time())
            if dsha(p[:112]) == h:
                return p
        raise P2PError("block not received")


def check_block(raw, want_hash):
    """-> (header dict, txs) after the hash and merkle checks."""
    if dsha(raw[:112]) != want_hash:
        raise P2PError("particld sent a different block")
    header, txs = L.parse_raw_block(raw, "part")
    if merkle_root([t["txid"] for t in txs]) != raw[36:68]:
        raise P2PError("merkle root mismatch: Particl parser and block disagree")
    return header, txs


class Headers:
    """Recent Particl headers by height (112 bytes each), linked, persisted.  first = height of the first stored header."""

    def __init__(self, path=STORE, keep_days=KEEP_DAYS):
        self.path, self.meta, self.keep = path, path.replace(".bin", ".json"), int(keep_days * NET["per_day"])
        self.first, self.h = 0, []
        try:
            with open(self.meta) as f:
                self.first = json.load(f)["first"]
            with open(path, "rb") as f:
                data = f.read()
            self.h = [data[i:i + 112] for i in range(0, len(data) - len(data) % 112, 112)]
        except (OSError, ValueError, KeyError):
            self.first, self.h = 0, []

    @property
    def tip(self):
        return self.first + len(self.h) - 1

    def hash(self, height):
        return dsha(self.h[height - self.first])

    def time(self, height):
        return struct.unpack("<I", self.h[height - self.first][100:104])[0]

    def locator(self):
        if not self.h:
            return [GENESIS]
        out, step, i = [], 1, len(self.h) - 1
        while i >= 0 and len(out) < 30:
            out.append(dsha(self.h[i]))
            i -= step
            if len(out) > 8:
                step *= 2
        return out + ([GENESIS] if self.first == 0 else [])

    def sync(self, peer, max_seconds=600, log=None):
        """Fetch new headers until the peer's tip; follows a reorganisation back to the fork point."""
        t0, added = time.time(), 0
        if not self.h:
            self.first = 0
        while time.time() - t0 < max_seconds:
            got = peer.getheaders(self.locator())
            if not got:
                break
            prev = got[0][4:36]
            if self.h and prev != dsha(self.h[-1]):                    # fork: find where the new run attaches
                at = next((i for i in range(len(self.h) - 1, -1, -1) if dsha(self.h[i]) == prev), None)
                if at is None:
                    raise P2PError("PART headers do not attach to the stored chain")
                if log:
                    log.warning("PART: reorganisation, %d stored headers replaced", len(self.h) - 1 - at)
                del self.h[at + 1:]
            elif not self.h and prev != GENESIS:
                raise P2PError("first PART header does not follow the genesis block")
            for a, b in zip(got, got[1:]):
                if b[4:36] != dsha(a):
                    raise P2PError("PART headers not linked")
            if not self.h:
                self.first = 1
            self.h.extend(got)
            added += len(got)
            if len(self.h) > self.keep + 20_000:                          # trim while syncing from genesis (memory)
                cut = len(self.h) - self.keep
                self.first += cut
                del self.h[:cut]
            if len(got) < 2000:
                break
        if len(self.h) > self.keep:
            cut = len(self.h) - self.keep
            self.first += cut
            del self.h[:cut]
        if added:
            self.save()
        return added

    def save(self):
        tmp = self.path + ".%d.tmp" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(b"".join(self.h))
        os.replace(tmp, self.path)
        tmp = self.meta + ".%d.tmp" % os.getpid()
        with open(tmp, "w") as f:
            json.dump({"first": self.first, "tip": self.tip, "updated": int(time.time())}, f)
        os.replace(tmp, self.meta)


class PartSource:
    """Block source for chain_swap_scan (same shape as RpcSource / P2PAdapter)."""

    def __init__(self, log=None):
        self.log, self.lat, self.peer = log, [], None
        self.chain = Headers()
        self._peer()
        self.chain.sync(self.peer, log=log)

    def _peer(self):
        for attempt in range(3):                     # a burst of local clients can get a connection reset: retry briefly (2026-11-20)
            if self.peer is not None:
                break
            try:
                self.peer = LocalPeer()
            except (OSError, P2PError):
                if attempt == 2:
                    raise
                time.sleep(2 + 3 * attempt)
        return self.peer

    def close(self):
        if self.peer:
            self.peer.close()
            self.peer = None

    def tip(self):
        self.chain.sync(self._peer(), log=self.log)
        return self.chain.tip

    def tip_time(self):
        return self.chain.time(self.chain.tip)

    def earliest(self, want, tip):
        return max(want, self.chain.first)

    def blockhash(self, h):
        return self.chain.hash(h)[::-1].hex()

    def block(self, h, bh_hex=None):
        t0 = time.time()
        want = self.chain.hash(h)
        try:
            raw = self._peer().getblock(want)
        except (OSError, P2PError):
            self.close()
            raw = self._peer().getblock(want)
        out = check_block(raw, want)
        self.lat.append(time.time() - t0)
        del self.lat[:-50]
        return out
