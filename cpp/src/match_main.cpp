// `bsx-swap-tracker match ...`: offer match (port of python/collectors/offer_match_live.py, 2026-11-20). A completed or refunded swap lock (strong lock
// spend / certain refund on BTC, LTC, PART) is matched to the public offer whose full amount on that coin equals the lock (+-2 sat) and that was
// listed around the lock time (first seen <= lock + 600 s, last seen >= lock - 3600 s): maker, direction, price, terms. Offers come from the
// board snapshots in snapshots_dir (first / last seen = snapshot file times, fields from the first snapshot that lists the offer). Price columns
// (prem_create_pct, prem_lock_pct, usd) stay empty: this runtime carries no market prices.
// Also matches the early lock flags of the live watcher (data_dir/early_locks.csv, last 48 h) with their status from the swap database.
// Writes <out-dir>/offer_match.csv and <out-dir>/offer_match_early.csv (atomically). Read-only towards the database and the snapshots.
//   bsx-swap-tracker [--config F] match [--days 2] [--context-days 2] [--out-dir DIR] [--db FILE]
#include <algorithm>
#include <unistd.h>
#include <charconv>
#include <cmath>
#include <dirent.h>
#include <map>
#include <optional>
#include <tuple>
#include <vector>
#include <sqlite3.h>
#include <zlib.h>
#include "json.hpp"
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

namespace {

const char* FIELDS[] = {"outcome", "direction", "coin", "lock_sat", "lock_time", "spend_time", "txid", "msg_id", "cf", "ct", "amt_from", "amt_to", "rate", "ttl",
                        "lock_type", "lock_value", "aa", "pv", "created", "first_seen", "last_seen", "addr", "mn", "frf", "frt", "prem_create_pct",
                        "prem_lock_pct", "usd", "age_at_lock_h", "lock_to_spend_h", "same_terms_24h"};
const char* EARLY_EXTRA[] = {"lock_vout", "first_stage", "height", "status"};

std::string pyfloat(double v) {                                  // Python repr of a float (shortest round trip, ".0" for integral values)
    char b[64];
    auto r = std::to_chars(b, b + sizeof b, v);
    std::string s(b, r.ptr);
    if (s.find_first_of(".en") == std::string::npos) s += ".0";
    return s;
}

std::string round2(double x) { return pyfloat(std::nearbyint(x * 100.0) / 100.0); }

struct Offer {
    std::string mid;
    int64_t first = 0, last = 0;
    std::map<std::string, std::string> f;                         // column -> CSV text ("" = None)
    std::optional<int64_t> created, a_from, a_to;
    std::string cf, ct;
};

std::string jtext(const Json* v, bool as_int, bool as_float) {   // offer_match_live.cv() for one field
    if (!v || v->is_null()) return "";
    if (as_int) {
        if (v->t == Json::T::Int) return std::to_string(v->i);
        if (v->t == Json::T::Num) return std::to_string(int64_t(v->d));
        if (v->t == Json::T::Str) { try { return std::to_string(v->s.find('.') != std::string::npos ? int64_t(std::stod(v->s)) : std::stoll(v->s)); } catch (...) { return ""; } }
        return "";
    }
    if (as_float) {
        if (v->t == Json::T::Int) return pyfloat(double(v->i));
        if (v->t == Json::T::Num) return pyfloat(v->d);
        if (v->t == Json::T::Str) { try { return pyfloat(std::stod(v->s)); } catch (...) { return ""; } }
        return "";
    }
    if (v->t == Json::T::Str) return v->s;
    if (v->t == Json::T::Bool) return v->b ? "True" : "False";
    return json_dump(*v);
}

std::optional<int64_t> as_i(const std::string& s) { if (s.empty()) return std::nullopt; try { return std::stoll(s); } catch (...) { return std::nullopt; } }

std::map<std::string, Offer> load_offers(const std::string& dir, int64_t since, int64_t until) {
    // raw field -> output column, as OFFER_COLS of offer_match_live.py
    static const std::vector<std::tuple<const char*, const char*, int>> COLS = {   // kind 1 int, 2 float, 0 text
        {"timestamp", "created", 1}, {"coin_from", "cf", 0}, {"coin_to", "ct", 0}, {"amount_from", "a_from", 1}, {"amount_to", "a_to", 1},
        {"amount_from_str", "amt_from", 2}, {"amount_to_str", "amt_to", 2}, {"rate", "rate", 2}, {"time_valid", "ttl", 1}, {"lock_type", "lock_type", 1},
        {"lock_value", "lock_value", 1}, {"auto_accept_type", "aa", 1}, {"protocol_version", "pv", 1}, {"min_bid_amount_str", "mn", 2},
        {"fee_rate_from", "frf", 1}, {"fee_rate_to", "frt", 1}, {"addr_from", "addr", 0}};
    std::vector<std::pair<int64_t, std::string>> files;
    if (DIR* d = opendir(dir.c_str())) {
        while (dirent* e = readdir(d)) {
            std::string n = e->d_name;
            if (n.size() < 24 || n.substr(n.size() - 8) != ".json.gz") continue;
            struct tm t{};
            if (!strptime(n.substr(0, 16).c_str(), "%Y%m%dT%H%M%SZ", &t)) continue;
            int64_t ts = int64_t(timegm(&t));
            if (ts >= since && ts <= until) files.emplace_back(ts, n);
        }
        closedir(d);
    }
    std::sort(files.begin(), files.end());
    std::map<std::string, Offer> idx;
    for (auto& [ts, n] : files) {
        gzFile g = gzopen((dir + "/" + n).c_str(), "rb");
        if (!g) continue;
        std::string text;
        char buf[65536];
        int r;
        while ((r = gzread(g, buf, sizeof buf)) > 0) text.append(buf, size_t(r));
        gzclose(g);
        if (r < 0) continue;
        Json snap;
        try { snap = json_parse(text); } catch (...) { continue; }
        const Json* offers = snap.get("offers");
        if (!offers || offers->t != Json::T::Arr) continue;
        for (auto& o : *offers->a) {
            const Json* mid = o.get("msg_id");
            if (!mid || mid->t != Json::T::Str) continue;
            auto it = idx.find(mid->s);
            if (it != idx.end()) { it->second.first = std::min(it->second.first, ts); it->second.last = std::max(it->second.last, ts); continue; }
            Offer of;
            of.mid = mid->s;
            of.first = of.last = ts;
            for (auto& [raw, col, kind] : COLS) of.f[col] = jtext(o.get(raw), kind == 1, kind == 2);
            of.created = as_i(of.f["created"]);
            of.a_from = as_i(of.f["a_from"]);
            of.a_to = as_i(of.f["a_to"]);
            of.cf = of.f["cf"];
            of.ct = of.f["ct"];
            idx.emplace(of.mid, std::move(of));
        }
    }
    return idx;
}

struct Ev { std::string coin, outcome; int64_t lock_sat, lock_time; std::optional<int64_t> spend_time; std::string txid; };

using Row = std::map<std::string, std::string>;

std::vector<Row> match(const std::vector<Ev>& events, const std::map<std::string, Offer>& idx) {
    std::map<std::pair<std::string, std::string>, std::vector<std::pair<int64_t, std::string>>> by;
    static const char* TERMS[] = {"cf", "ct", "ttl", "lock_value", "aa", "pv", "frf", "frt"};
    std::map<std::string, std::vector<int64_t>> terms;
    auto terms_key = [&](const Offer& o, std::string& k) {
        k.clear();
        for (const char* t : TERMS) { auto it = o.f.find(t); if (it == o.f.end() || it->second.empty()) return false; k += it->second + "|"; }
        return true;
    };
    std::string tk;
    for (auto& [mid, o] : idx) {
        if (!o.ct.empty() && o.a_to) by[{o.ct, "to"}].emplace_back(*o.a_to, mid);
        if (!o.cf.empty() && o.a_from) by[{o.cf, "from"}].emplace_back(*o.a_from, mid);
        if (terms_key(o, tk) && o.created) terms[tk].push_back(*o.created);
    }
    for (auto& [k, v] : by) std::sort(v.begin(), v.end());
    for (auto& [k, v] : terms) std::sort(v.begin(), v.end());
    std::vector<std::pair<int64_t, Row>> out;
    for (auto& e : events) {
        std::vector<std::pair<std::string, std::string>> hits;   // (msg_id, direction) in insertion order, "to" side wins
        for (const char* side : {"to", "from"}) {
            auto it = by.find({e.coin, side});
            if (it == by.end()) continue;
            auto& arr = it->second;
            auto lo = std::lower_bound(arr.begin(), arr.end(), std::make_pair(e.lock_sat - 2, std::string()));
            for (; lo != arr.end() && lo->first <= e.lock_sat + 2; ++lo) {
                const Offer& o = idx.at(lo->second);
                if (o.first <= e.lock_time + 600 && o.last >= e.lock_time - 3600) {
                    auto h = std::find_if(hits.begin(), hits.end(), [&](auto& x) { return x.first == lo->second; });
                    if (std::string(side) == "to") { if (h == hits.end()) hits.emplace_back(lo->second, "reverse"); else h->second = "reverse"; }
                    else if (h == hits.end()) hits.emplace_back(lo->second, "normal");
                }
            }
        }
        for (auto& [mid, dir] : hits) {
            const Offer& o = idx.at(mid);
            Row r;
            r["outcome"] = e.outcome; r["direction"] = dir; r["coin"] = e.coin; r["lock_sat"] = std::to_string(e.lock_sat);
            r["lock_time"] = std::to_string(e.lock_time); r["spend_time"] = e.spend_time ? std::to_string(*e.spend_time) : ""; r["txid"] = e.txid; r["msg_id"] = mid;
            for (const char* c : {"cf", "ct", "amt_from", "amt_to", "rate", "ttl", "lock_type", "lock_value", "aa", "pv", "created", "mn", "frf", "frt"}) r[c] = o.f.at(c);
            r["first_seen"] = std::to_string(o.first); r["last_seen"] = std::to_string(o.last);
            std::string a = o.f.at("addr");
            r["addr"] = a.substr(0, 6) + "...";
            r["prem_create_pct"] = r["prem_lock_pct"] = r["usd"] = "";
            r["age_at_lock_h"] = o.created ? round2(double(e.lock_time - *o.created) / 3600.0) : "";
            r["lock_to_spend_h"] = e.spend_time ? round2(double(*e.spend_time - e.lock_time) / 3600.0) : "";
            int64_t same = 0;
            if (terms_key(o, tk) && o.created) {
                auto& c = terms[tk];
                same = int64_t(std::upper_bound(c.begin(), c.end(), *o.created) - std::lower_bound(c.begin(), c.end(), *o.created - 86400));
            }
            r["same_terms_24h"] = std::to_string(same);
            out.emplace_back(e.lock_time, std::move(r));
        }
    }
    std::stable_sort(out.begin(), out.end(), [](auto& a, auto& b) { return a.first < b.first; });
    std::vector<Row> rows;
    for (auto& x : out) rows.push_back(std::move(x.second));
    return rows;
}

std::string csv_field(const std::string& s) {
    if (s.find_first_of(",\"\r\n") == std::string::npos) return s;
    std::string q = "\"";
    for (char c : s) { if (c == '"') q += '"'; q += c; }
    return q + "\"";
}

bool write_csv(const std::string& path, const std::vector<std::string>& cols, const std::vector<Row>& rows) {
    std::string out;
    for (size_t k = 0; k < cols.size(); k++) out += (k ? "," : "") + cols[k];
    out += "\r\n";
    for (auto& r : rows) {
        for (size_t k = 0; k < cols.size(); k++) { auto it = r.find(cols[k]); out += (k ? "," : "") + csv_field(it == r.end() ? "" : it->second); }
        out += "\r\n";
    }
    return write_atomic(path, out);
}

}  // namespace

int match_main(int argc, char** argv) {
    double days = 2, ctx_days = 2;
    std::string out_dir, db_path;
    for (int i = 2; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) throw std::invalid_argument(k + " needs a value"); return argv[++i]; };
        if (k == "--days") days = std::stod(val());
        else if (k == "--context-days") ctx_days = std::stod(val());
        else if (k == "--out-dir") out_dir = val();
        else if (k == "--db") db_path = val();
        else if (k == "--data-dir") override_data_dir(val());
        else if (k == "--snapshots-dir") override_snapshots_dir(val());
        else throw std::invalid_argument("unknown flag " + k);
    }
    if (out_dir.empty()) out_dir = settings().data_dir;
    if (db_path.empty()) db_path = settings().data_dir + "/chainscan.sqlite";
    int64_t now = now_i(), since = now - int64_t(days * 86400);
    double t0 = now_s();
    auto idx = load_offers(settings().snapshots_dir, since - int64_t(ctx_days * 86400), now - 15);
    sqlite3* db = nullptr;
    if (access(db_path.c_str(), R_OK) != 0) {
        std::fprintf(stderr, "match: no swap database at %s yet. Run `bsx-swap-tracker scan` first, or pass --db FILE.\n", db_path.c_str());
        return 1;
    }
    if (sqlite3_open_v2(db_path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) { fprintf(stderr, "match: cannot open %s\n", db_path.c_str()); return 1; }
    sqlite3_busy_timeout(db, 60000);
    std::vector<Ev> events;
    {
        sqlite3_stmt* st;
        sqlite3_prepare_v2(db, "select coin, case when kind='adaptor_refund_tx' then 'failed' else 'completed' end, amount_sat, lock_time, block_time, txid from swap_events "
                               "where coin in ('BTC','LTC','PART') and block_time >= ? and amount_sat is not null and lock_time is not null "
                               "and ((kind='adaptor_refund_tx' and confidence='certain') or (kind='adaptor_lock_spend' and confidence='strong'))", -1, &st, nullptr);
        sqlite3_bind_int64(st, 1, since);
        while (sqlite3_step(st) == SQLITE_ROW)
            events.push_back({reinterpret_cast<const char*>(sqlite3_column_text(st, 0)), reinterpret_cast<const char*>(sqlite3_column_text(st, 1)),
                              sqlite3_column_int64(st, 2), sqlite3_column_int64(st, 3), sqlite3_column_int64(st, 4), reinterpret_cast<const char*>(sqlite3_column_text(st, 5))});
        sqlite3_finalize(st);
    }
    auto rows = match(events, idx);
    std::vector<std::string> cols(std::begin(FIELDS), std::end(FIELDS));
    write_csv(out_dir + "/offer_match.csv", cols, rows);
    // early lock flags: flagged rows of the last 48 h, first sighting per (coin, txid, vout), status from the swap database
    std::map<std::tuple<std::string, std::string, int64_t>, std::tuple<int64_t, int64_t, std::string, std::string>> flags;   // -> (seen, value, stage, height)
    try {
        std::string text = read_text(settings().data_dir + "/early_locks.csv");
        std::vector<std::string> head;
        size_t pos = 0;
        auto next_line = [&](std::string& line) { if (pos >= text.size()) return false; size_t e = text.find('\n', pos); line = text.substr(pos, e == std::string::npos ? std::string::npos : e - pos); pos = e == std::string::npos ? text.size() : e + 1; if (!line.empty() && line.back() == '\r') line.pop_back(); return true; };
        auto split = [](const std::string& l) { std::vector<std::string> f; std::string cur; bool q = false; for (size_t i = 0; i < l.size(); i++) { char c = l[i]; if (q) { if (c == '"' && i + 1 < l.size() && l[i + 1] == '"') { cur += '"'; i++; } else if (c == '"') q = false; else cur += c; } else if (c == '"') q = true; else if (c == ',') { f.push_back(cur); cur.clear(); } else cur += c; } f.push_back(cur); return f; };
        std::string line;
        if (next_line(line)) head = split(line);
        auto col = [&](const std::vector<std::string>& f, const char* name) { for (size_t k = 0; k < head.size() && k < f.size(); k++) if (head[k] == name) return f[k]; return std::string(); };
        while (next_line(line)) {
            auto f = split(line);
            if (col(f, "flag") != "1") continue;
            std::string coin = col(f, "coin");
            if (coin != "BTC" && coin != "LTC" && coin != "PART") continue;
            struct tm t{};
            std::string su = col(f, "seen_utc");
            if (!strptime(su.substr(0, 19).c_str(), "%Y-%m-%dT%H:%M:%S", &t)) continue;
            int64_t seen = int64_t(timegm(&t));
            if (seen < now - 48 * 3600) continue;
            auto key = std::make_tuple(coin, col(f, "txid"), std::stoll(col(f, "vout")));
            auto it = flags.find(key);
            if (it == flags.end()) flags.emplace(key, std::make_tuple(seen, std::stoll(col(f, "value_sat")), col(f, "stage"), col(f, "height")));
            else if (!col(f, "height").empty()) std::get<3>(it->second) = col(f, "height");
        }
    } catch (...) {
    }
    std::map<std::tuple<std::string, std::string, int64_t>, std::pair<std::string, std::optional<int64_t>>> status;
    for (auto& [k, v] : flags) status[k] = {"pending", std::nullopt};
    if (!flags.empty()) {
        sqlite3_stmt* st;
        sqlite3_prepare_v2(db, "select coin, kind, confidence, block_time, prev_txid, prev_vout from swap_events where coin in ('BTC','LTC','PART') and block_time >= ? and prev_txid is not null", -1, &st, nullptr);
        sqlite3_bind_int64(st, 1, now - 48 * 3600 - 86400);
        while (sqlite3_step(st) == SQLITE_ROW) {
            auto key = std::make_tuple(std::string(reinterpret_cast<const char*>(sqlite3_column_text(st, 0))), std::string(reinterpret_cast<const char*>(sqlite3_column_text(st, 4))), sqlite3_column_int64(st, 5));
            auto it = status.find(key);
            if (it == status.end()) continue;
            std::string kind = reinterpret_cast<const char*>(sqlite3_column_text(st, 1)), conf = reinterpret_cast<const char*>(sqlite3_column_text(st, 2));
            it->second = {kind == "adaptor_lock_spend" && conf == "strong" ? "completed" : kind == "adaptor_refund_tx" ? "failed" : "spent:" + kind + "/" + conf, sqlite3_column_int64(st, 3)};
        }
        sqlite3_finalize(st);
    }
    sqlite3_close_v2(db);
    std::vector<Ev> fev;
    for (auto& [k, v] : flags) fev.push_back({std::get<0>(k), status[k].first, std::get<1>(v), std::get<0>(v), status[k].second, std::get<1>(k) + ":" + std::to_string(std::get<2>(k))});
    auto erows = match(fev, idx);
    std::map<std::tuple<std::string, std::string, int64_t>, bool> hit;
    for (auto& r : erows) {
        std::string tx = r["txid"];
        auto c = tx.rfind(':');
        auto key = std::make_tuple(r["coin"], tx.substr(0, c), std::stoll(tx.substr(c + 1)));
        hit[key] = true;
        r["txid"] = std::get<1>(key);
        r["lock_vout"] = std::to_string(std::get<2>(key));
        r["first_stage"] = std::get<2>(flags[key]);
        r["height"] = std::get<3>(flags[key]);
        r["status"] = status[key].first;
    }
    for (auto& [k, v] : flags) {
        if (hit.count(k)) continue;
        Row r;
        r["outcome"] = r["status"] = status[k].first; r["coin"] = std::get<0>(k); r["lock_sat"] = std::to_string(std::get<1>(v)); r["lock_time"] = std::to_string(std::get<0>(v));
        r["spend_time"] = status[k].second ? std::to_string(*status[k].second) : ""; r["txid"] = std::get<1>(k); r["lock_vout"] = std::to_string(std::get<2>(k));
        r["first_stage"] = std::get<2>(v); r["height"] = std::get<3>(v);
        erows.push_back(r);
    }
    std::stable_sort(erows.begin(), erows.end(), [](const Row& a, const Row& b) { return std::stoll(a.at("lock_time")) < std::stoll(b.at("lock_time")); });
    for (const char* x : EARLY_EXTRA) cols.push_back(x);
    write_csv(out_dir + "/offer_match_early.csv", cols, erows);
    printf("match: %zu offers from snapshots, %zu events since %s, %zu matched rows; %zu early flags (%zu rows); %.1f s\n", idx.size(), events.size(),
           utc_iso(since).c_str(), rows.size(), flags.size(), erows.size(), now_s() - t0);
    return 0;
}

}  // namespace bsx
