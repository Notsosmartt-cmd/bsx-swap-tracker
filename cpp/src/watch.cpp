// `bsx-swap-tracker watch ...`: the live chain watch (port of python/collectors/chain_watch.py). One long-lived connection per coin: BTC and LTC public peers
// and the local particld (P2P with relay: mempool and blocks), BCH by polling the local daemon's best block through the allowlisted RPC. Flags
// likely swap locks with the four lock-time rules, writes early_locks.csv / early_locks_latest.json / chainwatch_status.json in --out-dir, and
// (unless --no-trigger, as in shadow mode) starts the coin's scan unit 45 s and 180 s after a block and the offer match after a new flag
// (unit names from [watch] in the settings file).
#include <algorithm>
#include <chrono>
#include <csignal>
#include <cstring>
#include <deque>
#include <functional>
#include <optional>
#include <tuple>
#include <dirent.h>
#include <map>
#include <mutex>
#include <random>
#include <set>
#include <thread>
#include <unordered_set>
#include <unistd.h>
#include <zlib.h>
#include "config.hpp"
#include "json.hpp"
#include "net.hpp"
#include "scan.hpp"
#include "script.hpp"
#include "settings.hpp"
#include "source.hpp"
#include "util.hpp"

namespace bsx {

static const char* COLS[] = {"seen_utc", "stage", "coin", "txid", "vout", "value_sat", "height", "flag", "rule_lt0_v2", "rule_max2_outputs", "rule_not_round",
                             "n_offers", "offer_ids", "makers", "pairs", "tx_version", "tx_locktime", "n_outputs"};
static const std::set<std::string> WATCH_COINS = {"BTC", "LTC", "PART", "BCH"};

static void start_unit(const std::string& unit) {                  // unit names from the settings, validated there (valid_unit_name)
    if (!valid_unit_name(unit)) { LOG_WARN("refusing unit name " + unit); return; }
    std::string cmd = "systemctl --user start --no-block " + unit + " >/dev/null 2>&1";
    if (std::system(cmd.c_str()) != 0) LOG_WARN("could not start " + unit);
}

// ------------------------------------------------------------------ offers from the public board snapshots

struct Offer { std::string id, maker, pair; };

class Offers {
public:
    explicit Offers(std::string dir, int64_t window = 7200) : dir_(std::move(dir)), window_(window) {}
    size_t refresh() {
        int64_t now = now_i();
        std::set<std::string> keep;
        if (DIR* d = opendir(dir_.c_str())) {
            while (dirent* e = readdir(d)) {
                std::string n = e->d_name;
                if (n.size() < 20 || n.substr(n.size() - 8) != ".json.gz") continue;
                struct tm t{};
                if (!strptime(n.substr(0, 16).c_str(), "%Y%m%dT%H%M%SZ", &t)) continue;
                if (int64_t(timegm(&t)) >= now - window_) keep.insert(n);
            }
            closedir(d);
        }
        for (auto it = files_.begin(); it != files_.end();) it = keep.count(it->first) ? std::next(it) : files_.erase(it);
        for (auto& n : keep) {
            if (files_.count(n)) continue;
            std::vector<std::tuple<std::string, int64_t, Offer>> rows;
            try {
                gzFile g = gzopen((dir_ + "/" + n).c_str(), "rb");
                if (!g) continue;
                std::string text;
                char buf[65536];
                int r;
                while ((r = gzread(g, buf, sizeof buf)) > 0) text.append(buf, size_t(r));
                gzclose(g);
                if (r < 0) continue;
                Json snap = json_parse(text);
                const Json* offers = snap.get("offers");
                if (!offers || offers->t != Json::T::Arr) continue;
                for (auto& o : *offers->a) {
                    std::string mid = o.get("msg_id") ? o.get("msg_id")->as_str() : "";
                    Offer of{mid.size() > 16 ? mid.substr(mid.size() - 16) : mid, o.get("addr_from") ? o.get("addr_from")->as_str() : "",
                             (o.get("coin_from") ? o.get("coin_from")->as_str() : "None") + "/" + (o.get("coin_to") ? o.get("coin_to")->as_str() : "None")};
                    for (const char* side : {"from", "to"}) {
                        const Json* c = o.get(std::string("coin_") + side);
                        const Json* amt = o.get(std::string("amount_") + side);
                        if (c && WATCH_COINS.count(c->as_str()) && amt && amt->t == Json::T::Int && amt->i > 0) rows.emplace_back(c->as_str(), amt->i, of);
                    }
                }
            } catch (...) {
                continue;                                     // a half-written snapshot: retry next refresh
            }
            files_[n] = std::move(rows);
        }
        std::map<std::string, std::vector<std::pair<int64_t, std::string>>> idx;
        std::map<std::string, Offer> meta;
        for (auto& [n, rows] : files_)
            for (auto& [coin, amt, of] : rows) {
                std::string key = coin + "|" + of.id + "|" + std::to_string(amt);
                if (meta.emplace(key, of).second) idx[coin].emplace_back(amt, key);
            }
        for (auto& [c, v] : idx) std::sort(v.begin(), v.end());
        std::lock_guard<std::mutex> g(mx_);
        idx_ = std::move(idx);
        meta_ = std::move(meta);
        loaded_at_ = now_s();
        size_t total = 0;
        for (auto& [c, v] : idx_) total += v.size();
        return total;
    }
    std::vector<Offer> lookup(const std::string& coin, int64_t value) {
        std::lock_guard<std::mutex> g(mx_);
        std::vector<Offer> out;
        auto it = idx_.find(coin);
        if (it == idx_.end()) return out;
        auto lo = std::lower_bound(it->second.begin(), it->second.end(), std::make_pair(value - 2, std::string()));
        for (; lo != it->second.end() && lo->first <= value + 2; ++lo) out.push_back(meta_[lo->second]);
        return out;
    }
    size_t amounts() { std::lock_guard<std::mutex> g(mx_); size_t t = 0; for (auto& [c, v] : idx_) t += v.size(); return t; }
    size_t snapshots() const { return files_.size(); }
    double loaded_at() const { return loaded_at_; }

private:
    std::string dir_;
    int64_t window_;
    std::map<std::string, std::vector<std::tuple<std::string, int64_t, Offer>>> files_;
    std::map<std::string, std::vector<std::pair<int64_t, std::string>>> idx_;
    std::map<std::string, Offer> meta_;
    double loaded_at_ = 0;
    std::mutex mx_;
};

// ------------------------------------------------------------------ rules

struct Row { Json j; bool flag; };

static std::vector<Row> lock_matches(const Tx& tx, const std::string& coin, Offers& offers) {
    std::vector<Row> rows;
    if (tx.coinbase) return rows;
    Family fam = coin == "PART" ? Family::part : Family::btc;
    std::vector<std::string> types = coin == "BCH" ? std::vector<std::string>{"p2sh32", "p2sh"} : std::vector<std::string>{"p2wsh"};
    for (size_t i = 0; i < tx.vout.size(); i++) {
        const auto& o = tx.vout[i];
        if (!o.value) continue;
        std::string t = out_type(o.script).type;
        if (std::find(types.begin(), types.end(), t) == types.end()) continue;
        auto hits = offers.lookup(coin, int64_t(*o.value));
        if (hits.empty()) continue;
        bool r1 = tx.version == swap_tx_version(fam) && tx.locktime == 0;
        bool r2 = tx.vout.size() <= 2;
        bool r3 = (*o.value % 1000) != 0;
        std::set<std::string> ids, makers, pairs;
        for (auto& h : hits) { ids.insert(h.id); makers.insert(h.maker); pairs.insert(h.pair); }
        auto join3 = [](const std::set<std::string>& s, size_t lim) { std::string r; size_t k = 0; for (auto& x : s) { if (k++ == lim) break; if (!r.empty()) r += " "; r += x; } return r; };
        Json j = Json::object();
        j.set("txid", Json::str(hex_rev(tx.txid)));
        j.set("vout", Json::integer(int64_t(i)));
        j.set("value_sat", Json::integer(int64_t(*o.value)));
        bool flag = r1 && r2 && r3;
        j.set("flag", Json::integer(flag ? 1 : 0));
        j.set("rule_lt0_v2", Json::integer(r1 ? 1 : 0));
        j.set("rule_max2_outputs", Json::integer(r2 ? 1 : 0));
        j.set("rule_not_round", Json::integer(r3 ? 1 : 0));
        j.set("n_offers", Json::integer(int64_t(hits.size())));
        j.set("offer_ids", Json::str(join3(ids, 3)));
        j.set("makers", Json::str(join3(makers, 3)));
        j.set("pairs", Json::str(join3(pairs, 1000)));
        j.set("tx_version", Json::integer(tx.version));
        j.set("tx_locktime", Json::integer(tx.locktime));
        j.set("n_outputs", Json::integer(int64_t(tx.vout.size())));
        rows.push_back({j, flag});
    }
    return rows;
}

static std::optional<int64_t> coinbase_height(const Tx& tx) {
    if (tx.vin.empty()) return std::nullopt;
    const Bytes& s = tx.vin[0].script_sig;
    if (s.empty() || s[0] < 1 || s[0] > 8 || s.size() < size_t(1 + s[0])) return std::nullopt;
    int64_t v = 0;
    for (int k = 0; k < s[0]; k++) v |= int64_t(s[1 + k]) << (8 * k);
    return v;
}

// ------------------------------------------------------------------ output

class Sink {
public:
    explicit Sink(std::string dir) : dir_(std::move(dir)) {
        make_dirs(dir_);
        try { Json l = json_parse(read_text(dir_ + "/early_locks_latest.json")); if (const Json* r = l.get("rows")) if (r->t == Json::T::Arr) for (auto& x : *r->a) latest_.push_back(x); } catch (...) {}
    }
    std::function<void()> on_flag;
    int write(const std::string& coin, const std::string& stage, const std::vector<Row>& rows, std::optional<int64_t> height) {
        std::vector<Json> fresh;
        bool any_flag = false;
        {
            std::lock_guard<std::mutex> g(mx_);
            for (auto& r : rows) {
                std::string key = coin + "|" + stage + "|" + r.j.get("txid")->s + "|" + std::to_string(r.j.get("vout")->i);
                if (!done_.insert(key).second) continue;
                Json o = Json::object();
                o.set("seen_utc", Json::str(utc_iso(now_i()).substr(0, 19) + "+00:00"));
                o.set("stage", Json::str(stage));
                o.set("coin", Json::str(coin));
                o.set("height", height ? Json::integer(*height) : Json::str(""));
                for (auto& kv : *r.j.o) o.set(kv.first, kv.second);
                fresh.push_back(o);
                any_flag = any_flag || r.flag;
            }
            if (fresh.empty()) return 0;
            if (done_.size() > 100000) done_.clear();
            std::string path = dir_ + "/early_locks.csv";
            bool is_new = access(path.c_str(), F_OK) != 0;
            FILE* f = fopen(path.c_str(), "a");
            if (f) {
                if (is_new) { for (size_t k = 0; k < std::size(COLS); k++) fprintf(f, "%s%s", k ? "," : "", COLS[k]); fputs("\r\n", f); }
                for (auto& o : fresh) {
                    for (size_t k = 0; k < std::size(COLS); k++) {
                        const Json* v = o.get(COLS[k]);
                        std::string s = !v ? "" : v->t == Json::T::Str ? v->s : v->t == Json::T::Int ? std::to_string(v->i) : "";
                        bool quote = s.find_first_of(",\"\r\n") != std::string::npos;
                        if (quote) { std::string q; for (char ch : s) { if (ch == '"') q += '"'; q += ch; } s = "\"" + q + "\""; }
                        fprintf(f, "%s%s", k ? "," : "", s.c_str());
                    }
                    fputs("\r\n", f);                          // csv.writer line terminator
                }
                fclose(f);
            }
            for (auto& o : fresh) latest_.push_front(o);
            while (latest_.size() > 100) latest_.pop_back();
            Json doc = Json::object();
            doc.set("updated", Json::str(utc_iso(now_i()).substr(0, 19) + "+00:00"));
            Json arr = Json::array();
            for (auto& o : latest_) arr.push(o);
            doc.set("rows", arr);
            write_atomic(dir_ + "/early_locks_latest.json", json_dump(doc));
        }
        for (auto& o : fresh)
            LOG_INFO(fmt("%s %s %s:%lld %lld sat %s offers=%lld", o.get("flag")->i ? "FLAG" : "amount-match", coin.c_str(), o.get("txid")->s.substr(0, 16).c_str(),
                         (long long)o.get("vout")->i, (long long)o.get("value_sat")->i, stage.c_str(), (long long)o.get("n_offers")->i));
        if (any_flag && on_flag) on_flag();
        return int(fresh.size());
    }

private:
    std::string dir_;
    std::mutex mx_;
    std::unordered_set<std::string> done_;
    std::deque<Json> latest_;
};

// ------------------------------------------------------------------ watchers

struct Stats {
    std::string peer;
    std::string connected_since, last_msg_utc, last_error;
    int64_t blocks = 0, txs = 0, tx_parse_errors = 0, amount_matches = 0, flags = 0, reconnects = 0, scan_triggers = 0;
    Json last_block;
};

class Watcher {
public:
    Watcher(const CoinCfg& cfg, Offers& offers, Sink& sink, bool mempool, bool trigger) : cfg_(cfg), offers_(offers), sink_(sink), mempool_(mempool), trigger_(trigger) {}
    virtual ~Watcher() = default;
    void start() { th_ = std::thread([this] { pthread_setname_np(pthread_self(), ("watch-" + cfg_.coin).c_str()); run(); }); }
    void join() { if (th_.joinable()) th_.join(); }
    Json status() {
        std::lock_guard<std::mutex> g(mx_);
        Json s = Json::object();
        s.set("peer", st_.peer.empty() ? Json::null() : Json::str(st_.peer));
        s.set("connected_since", st_.connected_since.empty() ? Json::null() : Json::str(st_.connected_since));
        s.set("last_msg_utc", st_.last_msg_utc.empty() ? Json::null() : Json::str(st_.last_msg_utc));
        s.set("blocks", Json::integer(st_.blocks));
        s.set("last_block", st_.last_block);
        s.set("txs", Json::integer(st_.txs));
        s.set("tx_parse_errors", Json::integer(st_.tx_parse_errors));
        s.set("amount_matches", Json::integer(st_.amount_matches));
        s.set("flags", Json::integer(st_.flags));
        s.set("reconnects", Json::integer(st_.reconnects));
        s.set("last_error", st_.last_error.empty() ? Json::null() : Json::str(st_.last_error));
        s.set("scan_triggers", Json::integer(st_.scan_triggers));
        return s;
    }
    const std::string& coin() const { return cfg_.coin; }

protected:
    virtual void run() = 0;
    void after_block(const Hash32& bh, const std::vector<Tx>& txs, std::optional<int64_t> height) {
        std::vector<Row> rows;
        for (auto& t : txs) for (auto& r : lock_matches(t, cfg_.coin, offers_)) rows.push_back(r);
        int n = sink_.write(cfg_.coin, "block", rows, height);
        {
            std::lock_guard<std::mutex> g(mx_);
            st_.blocks++;
            Json lb = Json::object();
            lb.set("hash", Json::str(hex_rev(bh)));
            lb.set("height", height ? Json::integer(*height) : Json::null());
            lb.set("txs", Json::integer(int64_t(txs.size())));
            lb.set("at", Json::str(utc_iso(now_i()).substr(0, 19) + "+00:00"));
            lb.set("matches", Json::integer(int64_t(rows.size())));
            st_.last_block = lb;
            st_.amount_matches += n;
            for (auto& r : rows) st_.flags += r.flag;
        }
        LOG_INFO(fmt("%s: block %s height %s, %zu txs, %zu amount matches", cfg_.coin.c_str(), hex_rev(bh).c_str(), height ? std::to_string(*height).c_str() : "None", txs.size(), rows.size()));
        if (trigger_ && now_s() - last_trigger_ > 30) {
            last_trigger_ = now_s();
            std::string unit = settings().scan_units.at(cfg_.coin);
            for (int d : {45, 180})
                std::thread([this, unit, d] {
                    for (int k = 0; k < d && !g_stop; k++) std::this_thread::sleep_for(std::chrono::seconds(1));
                    if (g_stop) return;
                    start_unit(unit);
                    std::lock_guard<std::mutex> g(mx_);
                    st_.scan_triggers++;
                }).detach();
        }
    }
    void on_tx(const Bytes& payload) {
        Tx tx;
        try { Reader r(payload); tx = parse_tx(r, cfg_.family); }
        catch (...) { std::lock_guard<std::mutex> g(mx_); st_.tx_parse_errors++; return; }
        auto rows = lock_matches(tx, cfg_.coin, offers_);
        int n = rows.empty() ? 0 : sink_.write(cfg_.coin, "mempool", rows, std::nullopt);
        std::lock_guard<std::mutex> g(mx_);
        st_.txs++;
        st_.amount_matches += n;
        for (auto& r : rows) st_.flags += r.flag;
    }
    void set_error(const std::string& e) { std::lock_guard<std::mutex> g(mx_); st_.last_error = utc_iso(now_i()) + " " + e.substr(0, 180); st_.peer.clear(); st_.connected_since.clear(); }
    const CoinCfg& cfg_;
    Offers& offers_;
    Sink& sink_;
    bool mempool_, trigger_;
    double last_trigger_ = 0;
    std::mutex mx_;
    Stats st_;
    std::thread th_;
};

class P2PWatcher : public Watcher {
public:
    using Watcher::Watcher;

protected:
    std::unique_ptr<Peer> connect() {
        if (cfg_.mode == Mode::part_p2p) return std::make_unique<Peer>(cfg_, "127.0.0.1", cfg_.p2p_port, 15, true);   // local particld only
        static std::mt19937 rng(std::random_device{}());
        std::vector<std::string> names = cfg_.seeds;
        std::shuffle(names.begin(), names.end(), rng);
        for (auto& n : names) {
            auto addrs = resolve_ipv4(n, cfg_.p2p_port);
            std::shuffle(addrs.begin(), addrs.end(), rng);
            if (addrs.size() > 10) addrs.resize(10);
            for (auto& ip : addrs) {
                if (bad_.count(ip) || g_stop) continue;
                try { return std::make_unique<Peer>(cfg_, ip, cfg_.p2p_port, 15, true); }
                catch (...) { bad_.insert(ip); }
            }
        }
        if (bad_.size() > 200) bad_.clear();
        throw NetError("no peer reachable");
    }
    void session() {
        auto peer = connect();
        {
            std::lock_guard<std::mutex> g(mx_);
            st_.peer = peer->ip();
            st_.connected_since = utc_iso(now_i()).substr(0, 19) + "+00:00";
        }
        LOG_INFO(cfg_.coin + ": connected to " + peer->ip());
        had_msg_ = false;
        double last_msg = now_s(), last_ping = now_s();
        std::string cmd;
        Bytes p;
        size_t hl = header_len(cfg_.family);
        while (!g_stop) {
            bool got = peer->recv(cmd, p, 10);
            double now = now_s();
            if (now - last_ping > 300) {
                Bytes nonce(8);
                for (auto& b : nonce) b = uint8_t(rand());
                peer->send("ping", nonce);
                last_ping = now;
            }
            if (got) {
                last_msg = now;
                had_msg_ = true;
                std::lock_guard<std::mutex> g(mx_);
                st_.last_msg_utc = utc_iso(now_i()).substr(0, 19) + "+00:00";
            }
            if (now - last_msg > 900) throw NetError("peer silent for 15 min");
            if (!got) continue;
            if (cmd == "inv") {
                Reader r(p);
                uint64_t n = r.varint();
                Bytes want;
                size_t nwant = 0;
                for (uint64_t k = 0; k < n; k++) {
                    uint32_t t = r.u32();
                    auto h = r.read(32);
                    uint32_t ask = 0;
                    if (t == MSG_BLOCK || t == MSG_WITNESS_BLOCK) ask = MSG_WITNESS_BLOCK;
                    else if ((t == MSG_TX || t == MSG_WITNESS_TX) && mempool_) {
                        std::string key(reinterpret_cast<const char*>(h.data()), 32);
                        if (seen_.insert(key).second) { seen_order_.push_back(key); ask = MSG_WITNESS_TX; }
                    }
                    if (ask && nwant < 1000) {
                        want.insert(want.end(), reinterpret_cast<uint8_t*>(&ask), reinterpret_cast<uint8_t*>(&ask) + 4);
                        want.insert(want.end(), h.begin(), h.end());
                        nwant++;
                    }
                }
                while (seen_order_.size() > 100000) { seen_.erase(seen_order_.front()); seen_order_.pop_front(); }
                if (nwant) { Bytes pl; put_compact(pl, nwant); pl.insert(pl.end(), want.begin(), want.end()); peer->send("getdata", pl); }
            } else if (cmd == "headers") {
                Reader r(p);
                uint64_t n = r.varint();
                std::vector<Hash32> hs;
                for (uint64_t k = 0; k < n; k++) { auto h = r.read(hl); hs.push_back(sha256d(h)); r.varint(); }
                if (!hs.empty()) {
                    size_t from = hs.size() > 8 ? hs.size() - 8 : 0;
                    Bytes pl;
                    put_compact(pl, hs.size() - from);
                    for (size_t k = from; k < hs.size(); k++) { uint32_t t = MSG_WITNESS_BLOCK; pl.insert(pl.end(), reinterpret_cast<uint8_t*>(&t), reinterpret_cast<uint8_t*>(&t) + 4); pl.insert(pl.end(), hs[k].begin(), hs[k].end()); }
                    peer->send("getdata", pl);
                }
            } else if (cmd == "block") {
                if (p.size() < hl) continue;
                Hash32 bh = sha256d(View(p.data(), hl));
                if (cfg_.mode == Mode::p2p && !pow_ok(View(p.data(), 80), cfg_.scrypt)) throw NetError("block header fails its proof of work");
                Block b = parse_block(p, cfg_.family);
                Hash32 mr = merkle_root(b.txs);
                if (std::memcmp(mr.data(), p.data() + 36, 32)) throw NetError("merkle root mismatch");
                std::optional<int64_t> height;
                if (cfg_.mode == Mode::p2p && !b.txs.empty()) height = coinbase_height(b.txs[0]);
                after_block(bh, b.txs, height);
            } else if (cmd == "tx") {
                on_tx(p);
            }
        }
    }
    void run() override {
        int backoff = 5;
        while (!g_stop) {
            try { session(); }
            catch (const std::exception& e) {
                set_error(e.what());
                LOG_WARN(fmt("%s: %s; reconnecting in %d s", cfg_.coin.c_str(), e.what(), backoff));
            }
            for (int k = 0; k < backoff && !g_stop; k++) std::this_thread::sleep_for(std::chrono::seconds(1));
            { std::lock_guard<std::mutex> g(mx_); st_.reconnects++; }
            backoff = had_msg_ ? 5 : std::min(120, backoff * 2);
        }
    }
    bool had_msg_ = false;
    std::set<std::string> bad_;
    std::unordered_set<std::string> seen_;
    std::deque<std::string> seen_order_;
};

class RpcBlockWatcher : public Watcher {
public:
    using Watcher::Watcher;

protected:
    void run() override {
        std::unique_ptr<ChainRPC> rpc;
        std::string best;
        while (!g_stop) {
            try {
                if (!rpc) {
                    rpc = std::make_unique<ChainRPC>(make_rpc(cfg_, 60));
                    best = rpc->call("getbestblockhash").as_str();       // start from the current tip: the scan covers the history
                    std::lock_guard<std::mutex> g(mx_);
                    st_.peer = "local daemon";
                    st_.connected_since = utc_iso(now_i()).substr(0, 19) + "+00:00";
                }
                std::string h = rpc->call("getbestblockhash").as_str();
                { std::lock_guard<std::mutex> g(mx_); st_.last_msg_utc = utc_iso(now_i()).substr(0, 19) + "+00:00"; }
                if (h != best) {
                    best = h;
                    Json p = Json::array(); p.push(Json::str(h)); p.push(Json::integer(0));
                    std::string hexs = rpc->call("getblock", p).as_str();
                    Bytes raw(hexs.size() / 2);
                    for (size_t k = 0; k < raw.size(); k++) raw[k] = uint8_t(std::stoi(hexs.substr(2 * k, 2), nullptr, 16));
                    Hash32 bh = sha256d(View(raw.data(), 80));
                    if (hex_rev(bh) != h) throw NetError("daemon block does not hash to its id");
                    Block b = parse_block(raw, cfg_.family);
                    Hash32 mr = merkle_root(b.txs);
                    if (std::memcmp(mr.data(), raw.data() + 36, 32)) throw NetError("merkle root mismatch");
                    after_block(bh, b.txs, b.txs.empty() ? std::nullopt : coinbase_height(b.txs[0]));
                }
            } catch (const std::exception& e) {
                set_error(e.what());
                LOG_WARN(cfg_.coin + ": " + e.what());
                rpc.reset();
            }
            for (int k = 0; k < 15 && !g_stop; k++) std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
};

static void on_signal(int) { g_stop = true; }

int watch_main(int argc, char** argv) {
    std::vector<std::string> coins = {"BTC", "LTC", "PART", "BCH"};
    std::string out_dir, log_name = "chain_watch", lock_name;
    bool mempool = true, trigger = true;
    int seconds = 0;
    for (int i = 2; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) throw std::invalid_argument(k + " needs a value"); return argv[++i]; };
        if (k == "--coins") { coins.clear(); std::string v = val(), cur; for (char ch : v + ",") { if (ch == ',') { if (!cur.empty()) { coin_cfg(cur); coins.push_back(cur); } cur.clear(); } else cur += char(toupper(ch)); } }
        else if (k == "--out-dir") out_dir = val();
        else if (k == "--data-dir") override_data_dir(val());
        else if (k == "--snapshots-dir") override_snapshots_dir(val());
        else if (k == "--log-name") log_name = val();
        else if (k == "--lock-name") lock_name = val();
        else if (k == "--no-mempool") mempool = false;
        else if (k == "--no-trigger") trigger = false;
        else if (k == "--seconds") seconds = std::stoi(val());
        else throw std::invalid_argument("unknown flag " + k);
    }
    if (out_dir.empty()) out_dir = settings().data_dir;
    if (!lock_name.empty() && !single_instance(lock_name)) return 0;
    log_open(log_name);
    pthread_setname_np(pthread_self(), "MainThread");
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    make_dirs(out_dir);
    Offers offers(settings().snapshots_dir);
    Sink sink(out_dir);
    double last_match = 0;
    sink.on_flag = [&] {
        if (trigger && now_s() - last_match > 60) { last_match = now_s(); start_unit(settings().match_unit); }
    };
    LOG_INFO(fmt("offers: %zu amounts from %zu snapshots", offers.refresh(), offers.snapshots()));
    std::vector<std::unique_ptr<Watcher>> ws;
    for (auto& c : coins) {
        const CoinCfg& cfg = coin_cfg(c);
        if (cfg.mode == Mode::rpc_raw) ws.push_back(std::make_unique<RpcBlockWatcher>(cfg, offers, sink, false, trigger));
        else ws.push_back(std::make_unique<P2PWatcher>(cfg, offers, sink, mempool, trigger));
    }
    for (auto& w : ws) w->start();
    std::string started = utc_iso(now_i()).substr(0, 19) + "+00:00";
    double t0 = now_s(), last_off = now_s();
    while (!g_stop) {
        for (int k = 0; k < 10 && !g_stop; k++) std::this_thread::sleep_for(std::chrono::seconds(1));
        if (now_s() - last_off > 60) { offers.refresh(); last_off = now_s(); }
        Json doc = Json::object();
        doc.set("updated", Json::str(utc_iso(now_i()).substr(0, 19) + "+00:00"));
        doc.set("pid", Json::integer(getpid()));
        doc.set("started", Json::str(started));
        doc.set("impl", Json::str("c++"));
        doc.set("offers_amounts", Json::integer(int64_t(offers.amounts())));
        doc.set("offers_loaded_age_s", Json::integer(int64_t(now_s() - offers.loaded_at())));
        doc.set("snapshots", Json::integer(int64_t(offers.snapshots())));
        Json cs = Json::object();
        for (auto& w : ws) cs.set(w->coin(), w->status());
        doc.set("coins", cs);
        write_atomic(out_dir + "/chainwatch_status.json", json_dump(doc));
        if (seconds && now_s() - t0 >= seconds) { printf("%s\n", json_dump(doc).c_str()); g_stop = true; }
    }
    for (auto& w : ws) w->join();
    std::this_thread::sleep_for(std::chrono::milliseconds(1100));
    return 0;
}

}  // namespace bsx
