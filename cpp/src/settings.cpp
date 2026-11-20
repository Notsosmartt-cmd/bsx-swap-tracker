#include "settings.hpp"
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <unistd.h>
#include "util.hpp"

namespace bsx {

const std::set<int> DEFAULT_PROTECTED_PORTS = {12700, 11700, 19792, 20792, 29998, 34798, 9209};

namespace {

Settings g;
bool g_loaded = false;

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r"), b = s.find_last_not_of(" \t\r");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

std::string strip_comment(const std::string& s) {        // '#' outside a quoted string starts a comment
    bool q = false;
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '"') q = !q;
        if (s[i] == '#' && !q) return s.substr(0, i);
    }
    return s;
}

// Minimal TOML: [section], key = "string" | integer | true/false | [int, int, ...]. Enough for this file; anything else is an error.
std::map<std::string, std::string> parse_toml(const std::string& text, const std::string& name) {
    std::map<std::string, std::string> kv;
    std::string section, line;
    std::istringstream in(text);
    int n = 0;
    while (std::getline(in, line)) {
        n++;
        line = trim(strip_comment(line));
        if (line.empty()) continue;
        if (line.front() == '[' && line.back() == ']') { section = trim(line.substr(1, line.size() - 2)); continue; }
        auto eq = line.find('=');
        if (eq == std::string::npos) throw std::runtime_error(name + ":" + std::to_string(n) + ": expected key = value");
        kv[section + "." + trim(line.substr(0, eq))] = trim(line.substr(eq + 1));
    }
    return kv;
}

std::string as_string(const std::string& v, const std::string& key) {
    if (v.size() < 2 || v.front() != '"' || v.back() != '"') throw std::runtime_error("settings: " + key + " must be a quoted string");
    return v.substr(1, v.size() - 2);
}

std::set<int> as_int_list(const std::string& v, const std::string& key) {
    if (v.size() < 2 || v.front() != '[' || v.back() != ']') throw std::runtime_error("settings: " + key + " must be a list of integers");
    std::set<int> out;
    std::stringstream ss(v.substr(1, v.size() - 2));
    std::string item;
    while (std::getline(ss, item, ',')) {
        item = trim(item);
        if (item.empty()) continue;
        size_t used = 0;
        int x = std::stoi(item, &used);
        if (used != item.size() || x <= 0 || x > 65535) throw std::runtime_error("settings: bad port '" + item + "' in " + key);
        out.insert(x);
    }
    return out;
}

const char* COINS[] = {"BTC", "LTC", "PART", "BCH"};

void unit_defaults() {
    for (const char* c : COINS) g.scan_units[c] = std::string("bsx-swap-scan@") + c + ".service";
    g.match_unit = "bsx-swap-match.service";
}

std::string resolve(const std::string& root, const std::string& p) { return !p.empty() && p[0] == '/' ? p : root + "/" + p; }

void apply_defaults() {
    std::string r = root_dir();
    g.root = r;
    g.data_dir = r + "/data/analysis/chainscan";   // export-ok: built-in default layout
    g.snapshots_dir = r + "/data/basicswap/site_snapshots";   // export-ok: built-in default layout
    g.logs_dir = r + "/data/logs";
    g.locks_dir = r + "/data/locks";
    g.basicswap_json = r + "/coindata/basicswap.json";
    g.electrum_headers_dir = r + "/coindata/electrum_headers";
    g.protected_ports = DEFAULT_PROTECTED_PORTS;
    unit_defaults();
}

void load_file(const std::string& path) {
    auto kv = parse_toml(read_text(path), path);
    static const std::set<std::string> known = {"paths.root", "paths.data_dir", "paths.snapshots_dir", "paths.logs_dir", "paths.locks_dir",
                                                "paths.basicswap_json", "paths.electrum_headers_dir", "safety.protected_ports",
                                                "watch.scan_unit_btc", "watch.scan_unit_ltc", "watch.scan_unit_part", "watch.scan_unit_bch",
                                                "watch.match_unit"};
    for (auto& [k, v] : kv)
        if (!known.count(k)) throw std::runtime_error("settings: unknown key '" + k + "' in " + path);
    std::string root = kv.count("paths.root") ? resolve(root_dir(), as_string(kv["paths.root"], "paths.root")) : root_dir();
    g.root = root;
    auto path_of = [&](const char* key, std::string& dst, const char* dflt) {
        dst = resolve(root, kv.count(key) ? as_string(kv[key], key) : dflt);
    };
    path_of("paths.data_dir", g.data_dir, "data/analysis/chainscan");   // export-ok: built-in default layout
    path_of("paths.snapshots_dir", g.snapshots_dir, "data/basicswap/site_snapshots");   // export-ok: built-in default layout
    path_of("paths.logs_dir", g.logs_dir, "data/logs");
    path_of("paths.locks_dir", g.locks_dir, "data/locks");
    path_of("paths.basicswap_json", g.basicswap_json, "coindata/basicswap.json");
    path_of("paths.electrum_headers_dir", g.electrum_headers_dir, "coindata/electrum_headers");
    g.protected_ports = DEFAULT_PROTECTED_PORTS;
    if (kv.count("safety.protected_ports"))
        for (int p : as_int_list(kv["safety.protected_ports"], "safety.protected_ports")) g.protected_ports.insert(p);
    unit_defaults();
    for (const char* c : COINS) {
        std::string k = "watch.scan_unit_" + std::string(c);
        for (auto& ch : k) ch = char(tolower(ch));
        if (kv.count(k)) g.scan_units[c] = as_string(kv[k], k);
    }
    if (kv.count("watch.match_unit")) g.match_unit = as_string(kv["watch.match_unit"], "watch.match_unit");
    for (auto& [c, u] : g.scan_units) if (!valid_unit_name(u)) throw std::runtime_error("settings: bad unit name '" + u + "' for " + c);
    if (!valid_unit_name(g.match_unit)) throw std::runtime_error("settings: bad unit name '" + g.match_unit + "' in watch.match_unit");
    g.source = path;
}

}  // namespace

bool valid_unit_name(const std::string& u) {
    const std::string suf = ".service";
    if (u.size() <= suf.size() || u.size() > 200 || u.compare(u.size() - suf.size(), suf.size(), suf) != 0) return false;
    for (char c : u) if (!(isalnum((unsigned char)c) || c == '@' || c == '.' || c == '_' || c == '-')) return false;
    return true;
}

void load_settings(const std::string& explicit_path) {
    apply_defaults();
    std::string path = explicit_path;
    if (path.empty()) if (const char* e = getenv("BSX_SWAP_TRACKER_CONFIG")) path = e;
    if (path.empty())
        for (const char* c : {"/deploy/bsx-swap-tracker.toml", "/config/bsx-swap-tracker.toml"})
            if (access((root_dir() + c).c_str(), R_OK) == 0) { path = root_dir() + c; break; }
    if (!path.empty()) load_file(path);              // an explicit file that cannot be read or parsed is an error, never silently ignored
    g_loaded = true;
}

const Settings& settings() {
    if (!g_loaded) load_settings("");
    return g;
}

void override_data_dir(const std::string& dir) { settings(); g.data_dir = dir; }
void override_snapshots_dir(const std::string& dir) { settings(); g.snapshots_dir = dir; }

}  // namespace bsx
