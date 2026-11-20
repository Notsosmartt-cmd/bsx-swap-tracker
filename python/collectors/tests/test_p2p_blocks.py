"""Tests for python/collectors/p2p_blocks.py (framing, merkle root, header-window detection, handshake and getdata against a throw-away loopback peer).
   python3 python/collectors/tests/test_p2p_blocks.py"""
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # python/collectors/
import p2p_blocks as P  # noqa: E402


def _write_bytes(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def header(prev=b"\x00" * 32, merkle=b"\x11" * 32, t=1_700_000_000, bits=0x207FFFFF, nonce=0):
    return struct.pack("<i", 4) + prev + merkle + struct.pack("<IIII", t, bits, nonce, 0)[:12]


class Framing(unittest.TestCase):
    def test_message_round_trip_and_bad_checksum(self):
        a, b = socket.socketpair()
        net = P.NETS["BTC"]
        a.sendall(P.pack_msg(net["magic"], "ping", b"12345678"))
        peer = P.Peer.__new__(P.Peer)
        peer.net, peer.sock, peer.buf, peer.bytes_in, peer.timeout = net, b, b"", 0, 5
        self.assertEqual(peer.recv(), ("ping", b"12345678"))
        bad = bytearray(P.pack_msg(net["magic"], "ping", b"abcd"))
        bad[-1] ^= 1
        a.sendall(bytes(bad))
        with self.assertRaises(P.P2PError):
            peer.recv()
        a.close(); b.close()

    def test_version_payload_announces_nothing(self):
        v = P.version_payload(nonce=7)
        ver, services = P.parse_version(v)
        self.assertEqual((ver, services), (P.PROTOCOL, 0))     # no services offered
        self.assertEqual(v[-1], 0)                             # relay = false: no transaction announcements

    def test_merkle_root(self):
        t1 = "aa" * 32
        self.assertEqual(P.merkle_root([t1]), bytes.fromhex(t1)[::-1])
        t2 = "bb" * 32
        self.assertEqual(P.merkle_root([t1, t2]), P.dsha(bytes.fromhex(t1)[::-1] + bytes.fromhex(t2)[::-1]))
        self.assertEqual(len(P.merkle_root([t1, t2, "cc" * 32])), 32)                      # odd layer duplicates the last

    def test_pow_and_target(self):
        easy = header(bits=0x207FFFFF)
        self.assertEqual(P.bits_target(0x207FFFFF), 0x7FFFFF << (8 * (0x20 - 3)))
        found = None
        for n in range(200):
            h = easy[:76] + struct.pack("<I", n)
            if P.pow_ok(h, False):
                found = h
                break
        self.assertIsNotNone(found)
        self.assertFalse(P.pow_ok(header(bits=0x1D00FFFF), False))


class Headers(unittest.TestCase):
    def test_only_the_linked_recent_run_is_usable(self):
        hs, prev = [], b"\x00" * 32
        raw = b"\x00" * 80 * 5                                  # empty placeholders (the Electrum file is sparse)
        run = []
        for i in range(10):
            h = header(prev=prev, t=1_700_000_000 + i)
            prev = P.dsha(h)
            run.append(h)
        # break the link in the middle: the usable run starts after the break
        run[4] = header(prev=b"\x42" * 32, t=1_700_000_004)
        prev = P.dsha(run[4])
        for i in range(5, 10):
            run[i] = header(prev=prev, t=1_700_000_000 + i)
            prev = P.dsha(run[i])
        raw += b"".join(run)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(raw)
        try:
            c = P.HeaderChain(f.name)
            self.assertEqual((c.first, c.tip), (5 + 4, 14))
            self.assertEqual(c.hash(14), P.dsha(run[9]))
            with self.assertRaises(P.P2PError):
                c.header(c.first - 1)
        finally:
            os.unlink(f.name)


class Extended(unittest.TestCase):
    def test_cache_joined_to_the_live_file_extends_the_window(self):
        import struct as st
        run, prev = [], b"\x00" * 32
        for i in range(30):
            h = header(prev=prev, t=1_700_000_000 + i)
            prev = P.dsha(h)
            run.append(h)
        live_raw = b"\x00" * 80 * 13 + b"".join(run[10:])          # live file: thirteen empty slots, then real headers from run index 10 (height 13)
        cache_raw = st.pack("<I", 5) + b"".join(run[2:10])           # cache: heights 5..12 (run index 2..9)
        with tempfile.TemporaryDirectory() as d:
            live_path, cache_path = os.path.join(d, "live"), os.path.join(d, "cache")
            _write_bytes(live_path, live_raw)
            _write_bytes(cache_path, cache_raw)
            P.NETS["BTC"]["headers"], saved = live_path, P.NETS["BTC"]["headers"]   # absolute: os.path.join(electrum_headers_dir, abs) = abs
            try:
                e = P.ExtendedHeaders("BTC", cache_path)
                self.assertEqual((e.first, e.tip), (5, 13 + 20 - 1))
                self.assertEqual(e.hash(5), P.dsha(run[2]))
                self.assertEqual(e.hash(12), P.dsha(run[9]))
                self.assertEqual(e.hash(13), P.dsha(run[10]))
                _write_bytes(cache_path, st.pack("<I", 5) + b"".join(run[3:10]))      # cache no longer ends where the live file begins
                e.refresh()
                self.assertEqual(e.first, e.live.first)                  # a cache that does not join is ignored
            finally:
                P.NETS["BTC"]["headers"] = saved

    def test_getheaders_payload(self):
        p = P.getheaders_payload([b"\x01" * 32])
        self.assertEqual(len(p), 4 + 1 + 32 + 32)
        self.assertEqual(p[4], 1)


def serve(handler):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    def run():
        conn, _ = srv.accept()
        try:
            handler(conn)
        finally:
            conn.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


class FakePeer(unittest.TestCase):
    def connect(self, handler):
        port = serve(handler)
        P.NETS["BTC"]["port"], saved = port, P.NETS["BTC"]["port"]
        try:
            return P.Peer("BTC", "127.0.0.1", timeout=5)
        finally:
            P.NETS["BTC"]["port"] = saved

    @staticmethod
    def reader(conn):
        buf = b""
        def read(n):
            nonlocal buf
            while len(buf) < n:
                buf += conn.recv(65536)
            out, buf = buf[:n], buf[n:]
            return out
        def msg():
            hdr = read(24)
            return hdr[4:16].rstrip(b"\0").decode(), read(struct.unpack("<I", hdr[16:20])[0])
        return msg

    def test_handshake_then_getdata_returns_the_block_and_pings_are_answered(self):
        blk = header() + b"\x01" + b"\x00" * 60
        h = P.dsha(blk[:80])
        def handler(conn):
            m = self.reader(conn)
            m()                                                           # their version
            ver = struct.pack("<iQq", 70016, P.NODE_NETWORK | P.NODE_WITNESS, 0) + b"\0" * 52 + b"\0" + struct.pack("<i", 1) + b"\0"
            conn.sendall(P.pack_msg(P.NETS["BTC"]["magic"], "version", ver) + P.pack_msg(P.NETS["BTC"]["magic"], "verack", b""))
            cmd, _ = m()
            assert cmd == "verack"
            cmd, payload = m()
            assert cmd == "getdata" and payload[1:5] == struct.pack("<I", P.MSG_BLOCK_WITNESS) and payload[5:37] == h
            conn.sendall(P.pack_msg(P.NETS["BTC"]["magic"], "ping", b"\x01" * 8) + P.pack_msg(P.NETS["BTC"]["magic"], "block", blk))
            cmd, payload = m()
            assert cmd == "pong" and payload == b"\x01" * 8
        peer = self.connect(handler)
        peer.handshake()
        self.assertEqual(peer.get_blocks([h]), {h: blk})
        peer.close()

    def test_non_archival_peer_is_rejected(self):
        def handler(conn):
            m = self.reader(conn)
            m()
            ver = struct.pack("<iQq", 70016, P.NODE_WITNESS, 0) + b"\0" * 52 + b"\0" + struct.pack("<i", 1) + b"\0"      # no NODE_NETWORK: pruned peer
            conn.sendall(P.pack_msg(P.NETS["BTC"]["magic"], "version", ver))
            try:
                m()
            except Exception:
                pass
        peer = self.connect(handler)
        with self.assertRaises(P.P2PError):
            peer.handshake()
        peer.close()


class PrefetcherTests(unittest.TestCase):
    """BlockPrefetcher with fake peers: order, completeness, failures, missing blocks, jumps, memory bound, shutdown (logic only, no network)"""

    class Peer:
        def __init__(self, world, fail_every=0, lacks=(), delay=0.0):
            self.world, self.fail_every, self.lacks, self.delay, self.calls, self.closed = world, fail_every, set(lacks), delay, 0, False

        def get_blocks(self, hashes):
            import time
            self.calls += 1
            if self.fail_every and self.calls % self.fail_every == 0:
                raise P.P2PError("peer closed the connection")
            time.sleep(self.delay)
            return {h: self.world[h] for h in hashes if h in self.world and h not in self.lacks}

        def close(self):
            self.closed = True

    def make(self, n=200, size=1000, **kw):
        hashes = {h: ("h%06d" % h).encode() for h in range(1000, 1000 + n)}
        world = {hs: (hs + b"|" + b"x" * size) for hs in hashes.values()}
        peers = []

        def connect():
            p = self.Peer(world, **kw.pop("peer_kw", {})) if False else self.Peer(world, **self.peer_kw)
            peers.append(p)
            return p
        self.peer_kw = kw.pop("peer_kw", {})
        pf = P.BlockPrefetcher(connect, lambda h: hashes[h], lambda: 1000 + n - 1, name="t", pause=lambda k: 0.01, **kw)
        return pf, hashes, peers

    def test_sequential_read_is_complete_and_in_order(self):
        pf, hashes, peers = self.make(workers=4, batch=7)
        pf.start()
        try:
            for h in range(1000, 1200):
                self.assertEqual(pf.get(h, timeout=10), hashes[h] + b"|" + b"x" * 1000)
        finally:
            pf.close()
        self.assertGreater(len(peers), 1)                                     # several peers were used
        self.assertEqual(pf.stats["blocks"] >= 200, True)

    def test_flaky_peers_and_lacking_blocks_still_deliver_everything(self):
        pf, hashes, peers = self.make(workers=3, batch=5, peer_kw=dict(fail_every=3, lacks=[("h%06d" % 1050).encode()]))
        # block 1050 is lacking on every peer: the consumer must time out on it, everything else must arrive
        pf.start()
        try:
            for h in range(1000, 1050):
                self.assertTrue(pf.get(h, timeout=10).startswith(hashes[h]))
            with self.assertRaises(P.P2PError):
                pf.get(1050, timeout=1.5)
            for h in range(1051, 1100):
                self.assertTrue(pf.get(h, timeout=10).startswith(hashes[h]))
        finally:
            pf.close()
        self.assertGreater(pf.stats["peer_errors"], 0)

    def test_jump_resets_and_memory_is_bounded(self):
        pf, hashes, peers = self.make(n=400, size=10000, workers=4, batch=8, max_bytes=100_000)
        pf.start()
        try:
            self.assertTrue(pf.get(1000, timeout=10).startswith(hashes[1000]))
            import time
            time.sleep(0.5)
            with pf.cv:
                self.assertLessEqual(pf.nbytes, 100_000 + 4 * 8 * 10_100)     # cap plus the first burst: every worker takes a batch before the mean block size is known
            self.assertTrue(pf.get(1300, timeout=10).startswith(hashes[1300]))   # jump forward
            self.assertTrue(pf.get(1005, timeout=10).startswith(hashes[1005]))   # and back
        finally:
            pf.close()

    def test_close_stops_workers(self):
        pf, hashes, peers = self.make(workers=3)
        pf.start()
        pf.get(1000, timeout=10)
        pf.close()
        self.assertTrue(all(not t.is_alive() for t in pf.threads))
        self.assertTrue(all(p.closed for p in peers))


if __name__ == "__main__":
    unittest.main(verbosity=1)
