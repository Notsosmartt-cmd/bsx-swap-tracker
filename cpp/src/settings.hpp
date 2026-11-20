// Runtime settings of the swap tracker (2026-11-20): paths and the protected-port list, from a small TOML file, so the code carries no
// host layout. Lookup: --config FILE, else $BSX_SWAP_TRACKER_CONFIG, else <root>/deploy/bsx-swap-tracker.toml, else
// <root>/config/bsx-swap-tracker.toml, else built-in defaults (the layout of a BasicSwap market-making checkout: data/..., coindata/...).
// Relative paths resolve against [paths] root (default: the repository root). See docs/configuration.md.
#pragma once
#include <map>
#include <set>
#include <string>

namespace bsx {

struct Settings {
    std::string source = "built-in defaults";   // which file was loaded
    std::string root;                           // base of relative paths
    std::string data_dir;                       // database, status, heartbeats, early locks, PART headers, header cache
    std::string snapshots_dir;                  // public board snapshots (*.json.gz) for offer matching and early detection
    std::string logs_dir, locks_dir;
    std::string basicswap_json;                 // BCH RPC credentials (chainclients.bitcoincash), optional
    std::string electrum_headers_dir;           // BasicSwap Electrum header files for BTC / LTC (optional once header sync exists)
    std::set<int> protected_ports;              // never contacted, whatever any config says
    std::map<std::string, std::string> scan_units;   // [watch] systemd user unit the watcher starts after a block, per coin
    std::string match_unit;                     // [watch] unit started after a new early flag (offer match)
};

// BasicSwap's own UI, WebSocket and wallet RPC defaults (UI 12700, WS 11700, Particl RPC 19792 / ZMQ 20792, XMR wallet 29998,
// WOW wallet 34798, DCR wallet 9209): always protected. [safety] protected_ports adds host-specific ones.
extern const std::set<int> DEFAULT_PROTECTED_PORTS;
// unit names are passed to systemctl: only [A-Za-z0-9@._-]+ ending in .service is accepted
bool valid_unit_name(const std::string& u);

const Settings& settings();                     // loads on first use with the default lookup
void load_settings(const std::string& explicit_path);   // call once, before settings(), when --config is given
void override_data_dir(const std::string& dir);
void override_snapshots_dir(const std::string& dir);

}  // namespace bsx
