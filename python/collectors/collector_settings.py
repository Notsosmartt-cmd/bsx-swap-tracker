"""Settings of the swap tracker, Python reference (2026-11-20): the same TOML file, lookup order, keys and defaults as the C++ runtime
(cpp/chainscan/src/settings.cpp), so neither carries the host layout in code.
Lookup: --config FILE on the command line, else $BSX_SWAP_TRACKER_CONFIG, else <root>/deploy/bsx-swap-tracker.toml, else
<root>/config/bsx-swap-tracker.toml, else built-in defaults (data/..., coindata/... under the repository root). Keys:
  [paths] root, data_dir, snapshots_dir, logs_dir, locks_dir, basicswap_json, electrum_headers_dir
  [safety] protected_ports = [...]   (added to BasicSwap's defaults, which are always protected)
  [watch] scan_unit_btc / _ltc / _part / _bch, match_unit   (systemd user units the live watcher starts; default bsx-swap-scan@<COIN>.service,
          bsx-swap-match.service; only [A-Za-z0-9@._-]+.service accepted)
Unknown keys and bad ports are errors; an explicit file that cannot be read is an error (never silently ignored)."""
import os, re, sys, tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
# BasicSwap's own UI / WebSocket / wallet RPC defaults: UI 12700, WS 11700, Particl RPC 19792 / ZMQ 20792, XMR wallet 29998, WOW wallet 34798, DCR wallet 9209
DEFAULT_PROTECTED_PORTS = frozenset({12700, 11700, 19792, 20792, 29998, 34798, 9209})
KEYS = {"paths": {"root", "data_dir", "snapshots_dir", "logs_dir", "locks_dir", "basicswap_json", "electrum_headers_dir"}, "safety": {"protected_ports"},
        "watch": {"scan_unit_btc", "scan_unit_ltc", "scan_unit_part", "scan_unit_bch", "match_unit"}}
COINS = ("BTC", "LTC", "PART", "BCH")
UNIT_RE = re.compile(r"[A-Za-z0-9@._-]+\.service")
DEFAULTS = {"data_dir": "data/analysis/chainscan", "snapshots_dir": "data/basicswap/site_snapshots", "logs_dir": "data/logs", "locks_dir": "data/locks",  # export-ok: defaults
            "basicswap_json": "coindata/basicswap.json", "electrum_headers_dir": "coindata/electrum_headers"}


class Settings:
    def __init__(self, source, root, paths, protected_ports, scan_units, match_unit):
        self.source, self.root, self.protected_ports = source, root, frozenset(protected_ports)
        self.scan_units, self.match_unit = dict(scan_units), match_unit
        for k, v in paths.items():
            setattr(self, k, v)

    def as_dict(self):
        return {"source": self.source, "root": self.root, **{k: getattr(self, k) for k in DEFAULTS}, "protected_ports": sorted(self.protected_ports),
                "scan_units": " ".join(f"{c}={self.scan_units[c]}" for c in sorted(self.scan_units)), "match_unit": self.match_unit}


def _resolve(root, p):
    return p if os.path.isabs(p) else os.path.join(root, p)


def load(path=None):
    if path is None:
        path = os.environ.get("BSX_SWAP_TRACKER_CONFIG")
    if not path:
        for c in ("deploy/bsx-swap-tracker.toml", "config/bsx-swap-tracker.toml"):
            if os.access(os.path.join(ROOT, c), os.R_OK):
                path = os.path.join(ROOT, c)
                break
    cfg, source = {}, "built-in defaults"
    if path:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
        source = path
        for sec, items in cfg.items():
            if sec not in KEYS or not isinstance(items, dict):
                raise ValueError("settings: unknown section %r in %s" % (sec, path))
            for k in items:
                if k not in KEYS[sec]:
                    raise ValueError("settings: unknown key '%s.%s' in %s" % (sec, k, path))
    paths = cfg.get("paths", {})
    root = _resolve(ROOT, paths["root"]) if "root" in paths else ROOT
    resolved = {k: _resolve(root, paths.get(k, d)) for k, d in DEFAULTS.items()}
    ports = set(DEFAULT_PROTECTED_PORTS)
    for p in cfg.get("safety", {}).get("protected_ports", []):
        if not isinstance(p, int) or not 0 < p <= 65535:
            raise ValueError("settings: bad port %r in safety.protected_ports" % (p,))
        ports.add(p)
    w = cfg.get("watch", {})
    units = {c: w.get("scan_unit_" + c.lower(), f"bsx-swap-scan@{c}.service") for c in COINS}
    match_unit = w.get("match_unit", "bsx-swap-match.service")
    for u in list(units.values()) + [match_unit]:
        if not isinstance(u, str) or len(u) > 200 or not UNIT_RE.fullmatch(u):
            raise ValueError("settings: bad unit name %r in [watch]" % (u,))
    return Settings(source, root, resolved, ports, units, match_unit)


def _argv_config():
    a = sys.argv
    for i, x in enumerate(a[:-1]):
        if x == "--config":
            return a[i + 1]
    return None


S = load(_argv_config())    # module-level settings for the collector modules (chain_swap_scan, chain_watch, part_p2p, p2p_blocks, offer_match_live)

if __name__ == "__main__":
    for k, v in S.as_dict().items():
        print("%-20s %s" % (k, " ".join(map(str, v)) if isinstance(v, list) else v))
