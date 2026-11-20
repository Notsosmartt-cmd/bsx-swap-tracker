// `bsx-swap-tracker scan ...`: the scan driver (port of chain_swap_scan.main / scan_coin / warm_up / Throttle). Same flags, same database, same
// heartbeat and status files, same lock-file protocol. Extra flag --out-dir (status, heartbeat, PART headers; default <data_dir>)
// so a shadow run can write next to production without touching it.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <map>
#include <thread>
#include "config.hpp"
#include "scan.hpp"
#include "source.hpp"
#include "store.hpp"
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

struct Args {
    std::vector<std::string> coins = default_coins();
    bool probe = false, earliest = false, no_extend_back = false, no_wide = false, dry_run = false, no_lock = false;
    int verify = 0;
    double backfill_days = -1, rps = 3.0;           // -1: not given (fresh database: FIRST_SCAN_DAYS; existing: keep its depth); -2: "max"
    int max_seconds = 3300, slice_seconds = 600, max_blocks = 0;
    std::string db, out_dir, lock_name = "chain_swap_scan", log_name = "chain_swap_scan";
};

static double median(std::vector<double> v) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    size_t n = v.size();
    return n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
}

struct Throttle {
    double gap, last = 0, base = -1;
    explicit Throttle(double rps) : gap(1.0 / rps) {}
    void wait() {
        double d = last + gap - now_s();
        if (d > 0) std::this_thread::sleep_for(std::chrono::microseconds(int64_t(d * 1e6)));
        last = now_s();
    }
    bool check(const std::vector<double>& lat) {      // latency tripwire for local daemons: True = back off
        if (lat.size() < 20) return false;
        if (base < 0) { base = std::max(median(lat), 0.05); return false; }
        std::vector<double> tail(lat.end() - 20, lat.end());
        return median(tail) > std::max(3 * base, 3.0);
    }
};

static std::string g_heartbeat;

static void warm_up(Store& db, Source& src, const CoinCfg& cfg, int64_t start, Throttle& thr, double t_end) {
    auto mn = db.min_height(cfg.coin);
    if (!mn || start <= *mn) return;
    int64_t lo = std::max(*mn, start - int64_t(cfg.bpd * WARMUP_DAYS));
    if (db.p2sh_count(cfg.coin, lo, start - 1) > 0) return;
    LOG_INFO(fmt("%s: re-indexing %lld blocks before height %lld (script-hash index was pruned there)", cfg.coin.c_str(), (long long)(start - lo), (long long)start));
    for (int64_t h = lo; h < start; h++) {
        if (g_stop || now_s() > t_end) break;
        heartbeat(g_heartbeat, cfg.coin, h, "warm-up");
        thr.wait();
        std::string bh = src.blockhash(h);
        Block b = src.block(h, bh);
        ScanResult r = scan_block(cfg.family, h, b.time, b.txs, nullptr, false, 300, true);
        db.retry([&] { db.begin(); db.insert_p2sh(cfg.coin, r.p2sh); db.commit(); });
    }
}

static std::string scan_coin(Store& db, const CoinCfg& cfg, const Args& a, double budget_s) {
    std::unique_ptr<Source> src;
    int64_t tip, tip_time;
    try {
        src = make_source(cfg, a.out_dir);
        tip = src->tip();
        tip_time = src->tip_time();
    } catch (const std::exception& e) {
        LOG_WARN(cfg.coin + ": source not available (" + e.what() + ")");
        return "unreachable";
    }
    struct Closer { Source* s; ~Closer() { if (s) s->close(); } } closer{src.get()};
    if (now_i() - tip_time > SYNCED_MAX_AGE_S) {
        LOG_INFO(fmt("%s: node still syncing (tip %lld is %.1f h old); not scanning", cfg.coin.c_str(), (long long)tip, (now_i() - tip_time) / 3600.0));
        return "syncing";
    }
    std::optional<int64_t> start;
    try {
        if (db.has_state(cfg.coin)) {
            for (auto& [h, bh] : db.last_blocks(cfg.coin, REORG_DEPTH)) {
                if (h <= tip && src->blockhash(h) != bh) {
                    LOG_WARN(fmt("%s: reorg at height %lld, rewinding", cfg.coin.c_str(), (long long)h));
                    db.retry([&] { db.rewind(cfg.coin, h); });
                }
            }
            if (auto r2 = db.max_height(cfg.coin)) start = *r2 + 1;
        }
    } catch (const SourceUnavailable& e) {
        LOG_WARN(cfg.coin + ": " + e.what());
        return "unreachable";
    }
    // How far back: --backfill-days N, or "max" (as deep as the source allows; the coin's depth_days cap if it has one). Without the flag
    // a fresh database starts FIRST_SCAN_DAYS back and an existing one keeps its depth (moves forward, finishes a queued older range,
    // never queues a new one), so a forgotten flag cannot start a months-long backfill.
    const bool given = a.backfill_days != -1;
    double days = a.backfill_days >= 0 ? a.backfill_days : (a.backfill_days == -2 ? double(cfg.depth_days) : FIRST_SCAN_DAYS);
    if (!start && !given) LOG_INFO(fmt("%s: no --backfill-days: first scan covers %.0f days (pass --backfill-days N or max to go further back)", cfg.coin.c_str(), FIRST_SCAN_DAYS));
    if (!start) {
        int64_t want = days > 0 ? std::max<int64_t>(0, tip - int64_t(cfg.bpd * days)) : 0;
        start = src->earliest(want, tip);
        LOG_INFO(fmt("%s: first scan starts at height %lld (tip %lld, %.1f days back)", cfg.coin.c_str(), (long long)*start, (long long)tip, double(tip - *start) / cfg.bpd));
    }
    Lookup lookup = [&](const std::string& txid, uint32_t vout) { return db.lookup(cfg.coin, txid, vout); };
    Throttle thr(cfg.mode == Mode::rpc_raw ? std::min(a.rps, cfg.rps) : cfg.rps);
    double t_end = now_s() + budget_s, last_commit = now_s(), t_slice = now_s();
    int done = 0, trips = 0;
    double tm_fetch = 0, tm_scan = 0, tm_write = 0, tm_wait = 0, tm_commit = 0;
    struct Range { int64_t a, b; bool back; };
    std::vector<Range> ranges;
    std::optional<Range> older;
    if (!a.no_extend_back && !a.dry_run) {
        int64_t lo_desired = src->earliest(days > 0 ? std::max<int64_t>(0, tip - int64_t(cfg.bpd * days)) : 0, tip);
        auto mn = db.min_height(cfg.coin);
        auto bf = db.backfill(cfg.coin);
        if (given && !bf && mn && lo_desired < *mn) {
            bf = std::make_pair(lo_desired, *mn - 1);
            db.retry([&] { db.begin(); db.set_backfill(cfg.coin, bf->first, bf->second); db.commit(); });
            LOG_INFO(fmt("%s: older range %lld-%lld added to the scan (%.1f days)", cfg.coin.c_str(), (long long)bf->first, (long long)bf->second, double(bf->second - bf->first) / cfg.bpd));
        }
        if (bf && bf->first <= bf->second) older = Range{bf->first, bf->second, true};
    }
    ranges.push_back({*start, tip, false});              // the tip first: new blocks never wait behind a long history backfill
    if (older) ranges.push_back(*older);
    if (cfg.mode != Mode::rpc_raw && !a.dry_run) {
        try { warm_up(db, *src, cfg, *start, thr, t_end); }
        catch (const SourceUnavailable& e) { LOG_WARN(cfg.coin + ": index warm-up skipped (" + e.what() + ")"); }
    }
    size_t ri = 0;
    int64_t h = ranges[0].a, rng_end = ranges[0].b;
    bool in_back = ranges[0].back;
    while (!g_stop) {
        heartbeat(g_heartbeat, cfg.coin, h, "");
        if (h > rng_end) {
            if (in_back) db.retry([&] { db.begin(); db.drop_backfill(cfg.coin); db.commit(); });
            if (++ri >= ranges.size()) break;
            h = ranges[ri].a; rng_end = ranges[ri].b; in_back = ranges[ri].back;
            continue;
        }
        if (now_s() > t_end) break;
        if (a.max_blocks && done >= a.max_blocks) break;
        if (src->is_rpc() && thr.check(src->lat)) {
            trips++;
            LOG_WARN(fmt("%s: RPC latency tripwire, backing off 30 s (%d)", cfg.coin.c_str(), trips));
            if (trips >= 3) return "partial";
            for (int k = 0; k < 30 && !g_stop; k++) std::this_thread::sleep_for(std::chrono::seconds(1));
            src->lat.clear();
            thr.base = -1;
            continue;
        }
        double t0 = now_s();
        thr.wait();
        tm_wait += now_s() - t0;
        std::string bh;
        Block blk;
        try {
            t0 = now_s();
            bh = src->blockhash(h);
            blk = src->block(h, bh);
            tm_fetch += now_s() - t0;
        } catch (const BlockPruned&) {
            db.retry([&] { db.begin(); db.add_gap(cfg.coin, h, "pruned"); db.commit(); });
            h++;
            continue;
        } catch (const SourceUnavailable& e) {
            LOG_WARN(fmt("%s: %s at height %lld; stopping this run", cfg.coin.c_str(), e.what(), (long long)h));
            return "partial";
        } catch (const ParseError& e) {
            LOG_ERROR(fmt("%s: could not parse block at height %lld (%s); skipping, recorded as gap", cfg.coin.c_str(), (long long)h, e.what()));
            db.retry([&] { db.begin(); db.add_gap(cfg.coin, h, "parse_error"); db.commit(); });
            h++;
            continue;
        }
        std::string prev = hex_rev(View(blk.header.data() + 4, 32));
        ScanResult res;
        db.retry([&] {
            double ta = now_s();
            ScanResult idx = scan_block(cfg.family, h, blk.time, blk.txs, nullptr, false, 300, true);
            double tb = now_s();
            db.begin();
            db.insert_p2sh(cfg.coin, idx.p2sh);                   // the block's own script-hash outputs first, so same-block spends resolve
            double tc = now_s();
            res = scan_block(cfg.family, h, blk.time, blk.txs, lookup, !a.no_wide, cfg.wide_max_hex, false);
            double td = now_s();
            if (a.dry_run) {
                db.rollback();
            } else {
                db.insert_block(cfg.coin, h, bh, blk.time, blk.txs.size(), prev, res);
                if (in_back) db.backfill_next(cfg.coin, h + 1);
                double tcm = now_s();
                db.commit();                                     // one transaction per block: short write locks for concurrent readers
                tm_commit += now_s() - tcm;
            }
            double te = now_s();
            tm_scan += (tb - ta) + (td - tc);
            tm_write += (tc - tb) + (te - td);
        });
        done++;
        if (!res.events.empty()) {
            std::string s;
            for (auto& e : res.events) { if (!s.empty()) s += ", "; s += e.kind + "/" + e.confidence; }
            LOG_INFO(fmt("%s: height %lld: %s", cfg.coin.c_str(), (long long)h, s.c_str()));
        }
        if ((done % 25 == 0 || now_s() - last_commit > 4) && !a.dry_run) {
            last_commit = now_s();
            db.retry([&] { if (in_back) { db.begin(); db.backfill_next(cfg.coin, h + 1); } db.commit(); });
        }
        h++;
    }
    if (in_back && !a.dry_run) db.retry([&] { db.begin(); db.backfill_next(cfg.coin, h); db.commit(); });
    if (!a.dry_run) db.retry([&] { db.begin(); db.finalize_run(cfg.coin, tip, cfg.bpd, cfg.p2sh_retain_days); });
    double el = std::max(now_s() - t_slice, 1e-9);
    LOG_INFO(fmt("%s: scanned %d blocks this run (start %lld, tip %lld); %.1f blocks/min; time split: fetch %.0f%%, parse+recognise %.0f%%, db write %.0f%%, commit %.0f%%, throttle %.0f%%",
                 cfg.coin.c_str(), done, (long long)*start, (long long)tip, done / el * 60, 100 * tm_fetch / el, 100 * tm_scan / el, 100 * tm_write / el, 100 * tm_commit / el, 100 * tm_wait / el));
    bool finished = ri >= ranges.size() || (ri == ranges.size() - 1 && h > rng_end);
    auto bf = db.backfill(cfg.coin);
    bool pending = bf && bf->first <= bf->second;
    return finished && !pending ? "caught_up" : "partial";
}

static int probe(const Args& a) {
    for (auto& c : a.coins) {
        const CoinCfg& cfg = coin_cfg(c);
        try {
            auto src = make_source(cfg, a.out_dir);
            int64_t tip = src->tip(), t = src->tip_time();
            printf("%-4s tip %lld, tip block %s (%.1f h old)\n", c.c_str(), (long long)tip, utc_iso(t).c_str(), (now_i() - t) / 3600.0);
            src->close();
        } catch (const std::exception& e) {
            printf("%-4s not reachable: %s\n", c.c_str(), e.what());
        }
    }
    return 0;
}

static int verify(const Args& a) {
    for (auto& c : a.coins) {
        const CoinCfg& cfg = coin_cfg(c);
        try {
            auto src = make_source(cfg, a.out_dir);
            int64_t tip = src->tip();
            size_t ntx = 0, nsp = 0;
            int64_t from = cfg.mode == Mode::p2p ? tip - a.verify - 1 : tip - a.verify;
            int64_t to = cfg.mode == Mode::p2p ? tip - 1 : tip + 1;
            for (int64_t h = from; h < to; h++) {
                std::string bh = src->blockhash(h);
                Block b = src->block(h, bh);
                ntx += b.txs.size();
                nsp += scan_block(cfg.family, h, b.time, b.txs, nullptr, true).spends.size();
            }
            printf("%-4s verified %lld blocks, %zu txs (hash, proof of work where applicable, merkle root); script-revealing spends: %zu\n", c.c_str(), (long long)(to - from), ntx, nsp);
            src->close();
        } catch (const std::exception& e) {
            printf("%-4s verify failed: %s\n", c.c_str(), e.what());
        }
    }
    return 0;
}

static void on_signal(int) { g_stop = true; }

int scan_main(int argc, char** argv) {
    Args a;
    a.db = "";
    a.out_dir = "";
    for (int i = 2; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) throw std::invalid_argument(k + " needs a value"); return argv[++i]; };
        if (k == "--coins") {
            a.coins.clear();
            std::string v = val(), cur;
            for (char ch : v + ",") { if (ch == ',') { if (!cur.empty()) { for (auto& x : cur) x = char(toupper(x)); coin_cfg(cur); a.coins.push_back(cur); } cur.clear(); } else if (ch != ' ') cur += ch; }
        } else if (k == "--probe") a.probe = true;
        else if (k == "--earliest") a.earliest = true;
        else if (k == "--verify") a.verify = std::stoi(val());
        else if (k == "--backfill-days") { std::string v = val(); a.backfill_days = v == "max" ? -2 : std::stod(v); if (a.backfill_days < 0 && v != "max") throw std::invalid_argument("--backfill-days must be >= 0 or max"); }
        else if (k == "--rps") a.rps = std::stod(val());
        else if (k == "--max-seconds") a.max_seconds = std::stoi(val());
        else if (k == "--slice-seconds") a.slice_seconds = std::stoi(val());
        else if (k == "--max-blocks") a.max_blocks = std::stoi(val());
        else if (k == "--no-extend-back") a.no_extend_back = true;
        else if (k == "--no-wide") a.no_wide = true;
        else if (k == "--dry-run") a.dry_run = true;
        else if (k == "--db") a.db = val();
        else if (k == "--out-dir") a.out_dir = val();
        else if (k == "--data-dir") override_data_dir(val());   // database and every output file (default: settings data_dir)
        else if (k == "--no-lock") a.no_lock = true;
        else if (k == "--lock-name") a.lock_name = val();
        else if (k == "--log-name") a.log_name = val();
        else throw std::invalid_argument("unknown flag " + k);
    }
    if (a.out_dir.empty()) a.out_dir = settings().data_dir;
    if (a.db.empty()) a.db = settings().data_dir + "/chainscan.sqlite";
    make_dirs(a.out_dir);
    if (a.probe) return probe(a);
    if (a.verify) return verify(a);
    if (a.earliest) {
        for (auto& c : a.coins) {
            try { auto s = make_source(coin_cfg(c), a.out_dir); int64_t t = s->tip(); printf("%-4s oldest usable height %lld of %lld\n", c.c_str(), (long long)s->earliest(0, t), (long long)t); }
            catch (const std::exception& e) { printf("%-4s not reachable: %s\n", c.c_str(), e.what()); }
        }
        return 0;
    }
    if (!a.no_lock && !single_instance(a.lock_name)) return 0;
    log_open(a.log_name);
    pthread_setname_np(pthread_self(), "MainThread");
    g_heartbeat = a.out_dir + "/heartbeat_" + a.lock_name + ".json";
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    Store db(a.db);
    heartbeat(g_heartbeat, "", -1, "start");
    std::map<std::string, std::string> states;
    double t_end = now_s() + a.max_seconds;
    while (now_s() < t_end && !g_stop) {
        bool progressed = false;
        for (auto& c : a.coins) {
            auto it = states.find(c);
            if (it != states.end() && it->second != "partial") continue;
            states[c] = scan_coin(db, coin_cfg(c), a, std::min<double>(a.slice_seconds, std::max(10.0, t_end - now_s())));
            progressed = progressed || states[c] == "partial";
            if (now_s() >= t_end || g_stop) break;
        }
        if (!progressed || a.max_blocks || a.dry_run) break;
    }
    db.write_status(a.out_dir + "/status.json", states);
    std::string s;
    for (auto& [k, v] : states) { if (!s.empty()) s += ", "; s += "'" + k + "': '" + v + "'"; }
    LOG_INFO("run finished: {" + s + "}");
    g_stop = true;                                     // ends the instance-lock thread (removes the lock file)
    std::this_thread::sleep_for(std::chrono::milliseconds(1100));
    return 0;
}

}  // namespace bsx
