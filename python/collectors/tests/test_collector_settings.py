"""Settings parity: the Python reference (python/collectors/collector_settings.py) and the C++ runtime (bin/bsx-swap-tracker settings) must read the
same file the same way.  Fake files only; the C++ part is skipped when the binary is missing.
Run:  python3 python/collectors/tests/test_collector_settings.py"""
import os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the collectors folder (either layout)
import collector_settings as CS  # noqa: E402


def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)
BIN = os.path.join(ROOT, "bin", "bsx-swap-tracker")


def cpp(path):
    r = subprocess.run([BIN, "--config", path, "settings"], capture_output=True, text=True, timeout=30)
    if r.returncode:
        return None, r.stderr
    d = {}
    for ln in r.stdout.splitlines():
        k, _, v = ln.partition(" ")
        d[k] = v.strip()
    d["protected_ports"] = sorted(int(x) for x in d["protected_ports"].split())
    return d, ""


class Settings(unittest.TestCase):
    def files(self):
        d = tempfile.mkdtemp()
        good = os.path.join(d, "good.toml")
        _write(good, '[paths]\nroot = "%s"\ndata_dir = "x/db"   # comment\nsnapshots_dir = "/abs/snaps"\n\n[safety]\nprotected_ports = [1111, 2222]\n' % d)
        bad_key = os.path.join(d, "bad_key.toml")
        _write(bad_key, '[paths]\ndatadir = "x"\n')
        bad_port = os.path.join(d, "bad_port.toml")
        _write(bad_port, '[safety]\nprotected_ports = [70000]\n')
        bad_unit = os.path.join(d, "bad_unit.toml")
        _write(bad_unit, '[watch]\nscan_unit_btc = "x.service; rm -rf /"\n')
        units = os.path.join(d, "units.toml")
        _write(units, '[watch]\nscan_unit_ltc = "my-ltc-scan.service"\nmatch_unit = "m@1.service"\n')
        self.extra = (bad_unit, units)
        return d, good, bad_key, bad_port

    def test_python(self):
        d, good, bad_key, bad_port = self.files()
        s = CS.load(good)
        self.assertEqual(s.data_dir, os.path.join(d, "x/db"))
        self.assertEqual(s.snapshots_dir, "/abs/snaps")
        self.assertEqual(s.logs_dir, os.path.join(d, "data/logs"))
        self.assertTrue({1111, 2222} <= s.protected_ports and CS.DEFAULT_PROTECTED_PORTS <= s.protected_ports)
        for f in (bad_key, bad_port, self.extra[0]):
            with self.assertRaises(ValueError):
                CS.load(f)
        u = CS.load(self.extra[1])
        self.assertEqual((u.scan_units["LTC"], u.scan_units["BTC"], u.match_unit), ("my-ltc-scan.service", "bsx-swap-scan@BTC.service", "m@1.service"))
        with self.assertRaises(OSError):
            CS.load(os.path.join(d, "missing.toml"))

    @unittest.skipUnless(os.access(BIN, os.X_OK), "bin/bsx-swap-tracker not built")
    def test_cpp_equals_python(self):
        d, good, bad_key, bad_port = self.files()
        for f in (good, self.extra[1], os.path.join(ROOT, "deploy", "bsx-swap-tracker.toml"), os.path.join(ROOT, "release", "swap-tracker", "config", "example.toml"),
                  os.path.join(ROOT, "config", "example.toml")):      # private layout, public layout
            if not os.path.exists(f):
                continue
            c, err = cpp(f)
            self.assertIsNotNone(c, err)
            p = CS.load(f).as_dict()
            for k in ("root", "data_dir", "snapshots_dir", "logs_dir", "locks_dir", "basicswap_json", "electrum_headers_dir"):
                self.assertEqual(os.path.normpath(c[k]), os.path.normpath(p[k]), (f, k))
            self.assertEqual(c["protected_ports"], p["protected_ports"], f)
            self.assertEqual(c["scan_units"], p["scan_units"], f)
            self.assertEqual(c["match_unit"], p["match_unit"], f)
        for f in (bad_key, bad_port, self.extra[0], os.path.join(d, "missing.toml")):
            self.assertIsNone(cpp(f)[0], f)


if __name__ == "__main__":
    unittest.main(verbosity=1)
