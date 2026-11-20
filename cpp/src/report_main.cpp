// `bsx-swap-tracker report ...`: aggregates of the swap database and the offer match (2026-11-20). Read-only towards both inputs.
// Writes into --out-dir (default <data_dir>/report):
//   daily_swaps.csv  per UTC day and chain: completed swaps (strong lock spends), refunds (certain refund txs), secret-hash redeems of ANY
//                    protocol (certain), BCH covenant spends (certain), offer-matched completed swaps and their USD volume (when the match
//                    file has a usd column; the C++ matcher leaves it empty, the Python matcher fills it); since 2026-11-20 only UNIQUE matches count
//                    (one candidate offer, non-round amount; python/collectors/offer_match_quality.py), with two more columns: completed swaps whose lock
//                    matched several offers (offer_matched_ambiguous) and whose only match has a round amount (offer_matched_round_amount)
//   chains.csv       per chain: totals, refund share, first and last day, lock-to-spend minutes p10 / p50 / p90 of completed swaps
//   pairs.csv        per offer pair and direction (uniquely offer-matched swaps): swaps, refunds, scripted-coin volume, median lock, median lock-to-spend
//   sizes.csv        per chain and lock-size decade (base units): completed, refunded, offer-matched
//   makers.csv       per maker address prefix (offer-matched swaps): swaps, volume per chain, share of matched swaps  [not with --public]
// --public: the rules of the published dataset: USD rounded to 100 and left empty below 3 matched swaps per chain-day, pair volume and
// medians left empty below 3 swaps, no makers.csv,
// today (incomplete) left out.
//   bsx-swap-tracker [--config F] report [--db FILE] [--match FILE] [--out-dir DIR] [--public]
#include <algorithm>
#include <unistd.h>
#include <array>
#include <optional>
#include <stdexcept>
#include <cmath>
#include <map>
#include <set>
#include <tuple>
#include <sqlite3.h>
#include <vector>
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

namespace {

std::string day_of(int64_t t) { return utc_iso(t).substr(0, 10); }

std::vector<std::string> split_csv(const std::string& l) {
    std::vector<std::string> f;
    std::string cur;
    bool q = false;
    for (size_t i = 0; i < l.size(); i++) {
        char c = l[i];
        if (q) { if (c == '"' && i + 1 < l.size() && l[i + 1] == '"') { cur += '"'; i++; } else if (c == '"') q = false; else cur += c; }
        else if (c == '"') q = true;
        else if (c == ',') { f.push_back(cur); cur.clear(); }
        else cur += c;
    }
    f.push_back(cur);
    return f;
}

std::vector<std::map<std::string, std::string>> read_csv(const std::string& path) {
    std::vector<std::map<std::string, std::string>> rows;
    std::string text;
    try { text = read_text(path); } catch (...) { return rows; }
    std::vector<std::string> head;
    size_t pos = 0;
    while (pos < text.size()) {
        size_t e = text.find('\n', pos);
        std::string line = text.substr(pos, e == std::string::npos ? std::string::npos : e - pos);
        pos = e == std::string::npos ? text.size() : e + 1;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        auto f = split_csv(line);
        if (head.empty()) { head = f; continue; }
        std::map<std::string, std::string> r;
        for (size_t k = 0; k < head.size(); k++) r[head[k]] = k < f.size() ? f[k] : "";
        rows.push_back(std::move(r));
    }
    return rows;
}

double quantile(std::vector<double> v, double q) {
    if (v.empty()) return NAN;
    std::sort(v.begin(), v.end());
    return v[size_t(q * double(v.size() - 1) + 0.5)];
}

std::string num(double x, int dec = 1) {
    if (std::isnan(x)) return "";
    char b[64];
    std::snprintf(b, sizeof b, "%.*f", dec, x);
    return b;
}

std::string csv_field(const std::string& s) {
    if (s.find_first_of(",\"\r\n") == std::string::npos) return s;
    std::string q = "\"";
    for (char c : s) { if (c == '"') q += '"'; q += c; }
    return q + "\"";
}

void write_table(const std::string& path, const std::vector<std::string>& cols, const std::vector<std::vector<std::string>>& rows) {
    std::string out;
    for (size_t k = 0; k < cols.size(); k++) out += (k ? "," : "") + cols[k];
    out += "\n";
    for (auto& r : rows) {
        for (size_t k = 0; k < r.size(); k++) out += (k ? "," : "") + csv_field(r[k]);
        out += "\n";
    }
    if (!write_atomic(path, out)) throw std::runtime_error("cannot write " + path);
}

std::optional<int64_t> to_i(const std::string& s) { try { size_t u; int64_t v = std::stoll(s, &u); return u == s.size() ? std::optional<int64_t>(v) : std::nullopt; } catch (...) { return std::nullopt; } }

}  // namespace

int report_main(int argc, char** argv) {
    std::string db_path, match_path, out_dir;
    bool pub = false;
    for (int i = 2; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) throw std::invalid_argument(k + " needs a value"); return argv[++i]; };
        if (k == "--db") db_path = val();
        else if (k == "--match") match_path = val();
        else if (k == "--out-dir") out_dir = val();
        else if (k == "--data-dir") override_data_dir(val());
        else if (k == "--public") pub = true;
        else throw std::invalid_argument("unknown flag " + k);
    }
    if (db_path.empty()) db_path = settings().data_dir + "/chainscan.sqlite";
    if (match_path.empty()) match_path = settings().data_dir + "/offer_match.csv";
    if (out_dir.empty()) out_dir = settings().data_dir + "/report";
    make_dirs(out_dir);
    const std::string today = day_of(now_i());
    const char* CHAINS[] = {"BTC", "LTC", "PART", "BCH"};

    // ---- events (top tiers only)
    struct Day { int64_t completed = 0, refunded = 0, htlc = 0, covenant = 0, matched = 0, priced = 0, ambiguous = 0, round_amt = 0, unpriced = 0,
                 htlc_swaps = 0, covenant_swaps = 0; double usd = 0; };
    std::map<std::pair<std::string, std::string>, Day> daily;          // (day, chain)
    struct Chain { int64_t completed = 0, refunded = 0, refund_pending = 0; std::string first, last; std::vector<double> lts; };
    std::map<std::string, Chain> chains;
    std::map<std::pair<std::string, int>, std::array<int64_t, 3>> sizes;   // (chain, decade) -> completed, refunded, matched
    std::map<std::string, std::pair<std::string, int>> size_of_tx;     // txid -> (chain, decade), for the matched count
    std::map<std::string, std::vector<std::pair<int64_t, std::string>>> htlc_legs;   // secret hash -> (redeem time, chain)
    std::vector<std::tuple<std::string, std::string, std::string, std::string>> covenant;   // (txid, prev txid, day, chain)
    sqlite3* db = nullptr;
    if (access(db_path.c_str(), R_OK) != 0) {
        std::fprintf(stderr, "report: no swap database at %s yet. Run `bsx-swap-tracker scan` first, or pass --db FILE.\n", db_path.c_str());
        return 1;
    }
    if (sqlite3_open_v2(db_path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) { std::fprintf(stderr, "report: cannot open %s\n", db_path.c_str()); return 1; }
    sqlite3_busy_timeout(db, 60000);
    sqlite3_stmt* st;
    sqlite3_prepare_v2(db, "select coin, kind, confidence, block_time, lock_time, amount_sat, txid, prev_txid, secret_hash from swap_events where coin in ('BTC','LTC','PART','BCH') and "
                           "((kind='adaptor_lock_spend' and confidence='strong') or (kind in ('adaptor_refund_tx','htlc_redeem','bch_covenant_spend') and confidence='certain'))",
                       -1, &st, nullptr);
    while (sqlite3_step(st) == SQLITE_ROW) {
        std::string coin = reinterpret_cast<const char*>(sqlite3_column_text(st, 0)), kind = reinterpret_cast<const char*>(sqlite3_column_text(st, 1));
        int64_t bt = sqlite3_column_int64(st, 3);
        std::string d = day_of(bt);
        if (pub && d >= today) continue;
        Day& dd = daily[{d, coin}];
        Chain& c = chains[coin];
        if (c.first.empty() || d < c.first) c.first = d;
        if (d > c.last) c.last = d;
        bool has_amt = sqlite3_column_type(st, 5) != SQLITE_NULL;
        int dec = has_amt && sqlite3_column_int64(st, 5) > 0 ? int(std::floor(std::log10(double(sqlite3_column_int64(st, 5))))) : -1;
        if (kind == "adaptor_lock_spend") {
            dd.completed++; c.completed++;
            if (sqlite3_column_type(st, 4) != SQLITE_NULL) c.lts.push_back(double(bt - sqlite3_column_int64(st, 4)) / 60.0);
            if (dec >= 0) { sizes[{coin, dec}][0]++; size_of_tx[reinterpret_cast<const char*>(sqlite3_column_text(st, 6))] = {coin, dec}; }
        } else if (kind == "adaptor_refund_tx") {
            dd.refunded++; c.refunded++;
            if (dec >= 0) sizes[{coin, dec}][1]++;
        } else if (kind == "htlc_redeem") {
            dd.htlc++;
            if (sqlite3_column_type(st, 8) != SQLITE_NULL) htlc_legs[reinterpret_cast<const char*>(sqlite3_column_text(st, 8))].push_back({bt, coin});
        } else {
            dd.covenant++;
            covenant.push_back({reinterpret_cast<const char*>(sqlite3_column_text(st, 6)),
                                sqlite3_column_type(st, 7) != SQLITE_NULL ? reinterpret_cast<const char*>(sqlite3_column_text(st, 7)) : "", d, coin});
        }
    }
    sqlite3_finalize(st);
    // secret-hash swaps (review item 5, 2026-11-20): the template is shared by other atomic-swap software, so these are never in the completed
    // totals; a secret hash redeemed on two scanned chains is one swap, counted once on the day and chain of its first redeem (the per-chain
    // secret_hash_redeems_any_protocol column counts each leg on its own chain, so summing it across chains counts such a swap twice)
    for (auto& [h, legs] : htlc_legs) {
        std::set<std::string> coins;
        for (auto& l : legs) coins.insert(l.second);
        if (coins.size() < 2) continue;
        auto first = *std::min_element(legs.begin(), legs.end());
        daily[{day_of(first.first), first.second}].htlc_swaps++;
    }
    // BCH covenant (review item 8): a refund goes through a second covenant, so one refunded swap is two spends; a swap = a covenant spend
    // whose input was not created by another covenant spend
    {
        std::set<std::string> cov_tx;
        for (auto& c : covenant) cov_tx.insert(std::get<0>(c));
        for (auto& c : covenant) if (!cov_tx.count(std::get<1>(c))) daily[{std::get<2>(c), std::get<3>(c)}].covenant_swaps++;
    }
    // refund transactions whose refund spend is not seen yet (newest days undercount refunds until then)
    sqlite3_prepare_v2(db, "select coin, count(*) from swap_events where kind='adaptor_refund_tx' and confidence='likely' group by coin", -1, &st, nullptr);
    while (sqlite3_step(st) == SQLITE_ROW) chains[reinterpret_cast<const char*>(sqlite3_column_text(st, 0))].refund_pending = sqlite3_column_int64(st, 1);
    sqlite3_finalize(st);
    sqlite3_close_v2(db);

    // ---- offer match: one row per swap.  A lock can match more than one offer (same amount): until 2026-11-20 the first row counted, in an order
    // that came from an unordered join.  Now a lock counts towards pairs, makers, sizes and USD only when its match is UNIQUE: one candidate offer
    // and a non-round amount (a multiple of 1,000 base units matches by chance far more often; same rule as python/collectors/offer_match_quality.py and
    // the recall check).  Ambiguous and round-amount matches are counted per day, nothing else.
    auto match_rows = read_csv(match_path);
    std::map<std::pair<std::string, std::string>, std::set<std::string>> cands;   // (txid, outcome) -> distinct offers
    for (auto& r : match_rows) cands[{r["txid"], r["outcome"]}].insert(r["msg_id"]);
    int64_t n_ambiguous = 0, n_round = 0;
    struct Pair { int64_t swaps = 0, refunds = 0, sat = 0; std::vector<double> lock, lts; };
    std::map<std::pair<std::string, std::string>, Pair> pairs;          // ("cf/ct", direction)
    struct Maker { int64_t swaps = 0, refunds = 0; std::map<std::string, int64_t> sat; };
    std::map<std::string, Maker> makers;
    std::set<std::string> seen;
    int64_t matched_total = 0;
    for (auto& r : match_rows) {
        if (!seen.insert(r["txid"]).second) continue;
        auto lock = to_i(r["lock_sat"]);
        bool ambiguous = cands[{r["txid"], r["outcome"]}].size() > 1;
        bool round_amt = lock && *lock % 1000 == 0;
        if (ambiguous || round_amt) {
            (ambiguous ? n_ambiguous : n_round)++;
            if (r["outcome"] == "completed") {
                auto spend = to_i(r["spend_time"]);
                if (spend) {
                    std::string d = day_of(*spend);
                    if (!(pub && d >= today)) { Day& dd = daily[{d, r["coin"]}]; (ambiguous ? dd.ambiguous : dd.round_amt)++; }
                }
            }
            continue;
        }
        std::string pair = r["cf"] + "/" + r["ct"];
        Pair& p = pairs[{pair, r["direction"]}];
        if (r["outcome"] == "completed") {
            auto spend = to_i(r["spend_time"]);
            if (!spend) continue;
            std::string d = day_of(*spend);
            if (pub && d >= today) continue;
            Day& dd = daily[{d, r["coin"]}];
            dd.matched++;
            bool priced = false;
            if (!r["usd"].empty()) { try { dd.usd += std::stod(r["usd"]); dd.priced++; priced = true; } catch (...) {} }
            if (!priced) dd.unpriced++;                                 // review item 7: say how many matched swaps have no USD value
            auto it = size_of_tx.find(r["txid"]);
            if (it != size_of_tx.end()) sizes[it->second][2]++;
            p.swaps++;
            if (!r["lock_to_spend_h"].empty()) { try { p.lts.push_back(std::stod(r["lock_to_spend_h"]) * 60.0); } catch (...) {} }
        } else if (r["outcome"] == "failed") { p.refunds++; makers[r["addr"]].refunds++; continue; }   // review item 6: refunds are not volume
        else continue;
        matched_total++;
        if (lock) { p.sat += *lock; p.lock.push_back(double(*lock)); }
        Maker& m = makers[r["addr"]];
        m.swaps++;
        if (lock) m.sat[r["coin"]] += *lock;
    }

    std::vector<std::vector<std::string>> rows;
    for (auto& [k, d] : daily) {
        std::string usd;
        if (d.priced && (!pub || d.priced >= 3)) usd = pub ? std::to_string(int64_t(std::nearbyint(d.usd / 100.0) * 100)) : num(d.usd, 2);
        rows.push_back({k.first, k.second, std::to_string(d.completed), std::to_string(d.refunded), std::to_string(d.htlc), std::to_string(d.covenant),
                        std::to_string(d.matched), usd, std::to_string(d.ambiguous), std::to_string(d.round_amt), std::to_string(d.unpriced),
                        std::to_string(d.htlc_swaps), std::to_string(d.covenant_swaps)});
    }
    write_table(out_dir + "/daily_swaps.csv", {"day", "chain", "completed", "refunded", "secret_hash_redeems_any_protocol", "bch_covenant_spends", "offer_matched",
                                              "offer_matched_usd", "offer_matched_ambiguous", "offer_matched_round_amount", "offer_matched_unpriced",
                                              "secret_hash_swaps_two_chains", "bch_covenant_swaps"}, rows);
    rows.clear();
    for (const char* c : CHAINS) {
        auto it = chains.find(c);
        if (it == chains.end()) continue;
        auto& x = it->second;
        double share = x.completed + x.refunded ? double(x.refunded) / double(x.completed + x.refunded) : NAN;
        rows.push_back({c, std::to_string(x.completed), std::to_string(x.refunded), num(share * 100, 2), x.first, x.last,
                        num(quantile(x.lts, 0.1)), num(quantile(x.lts, 0.5)), num(quantile(x.lts, 0.9)), std::to_string(x.refund_pending)});
    }
    write_table(out_dir + "/chains.csv", {"chain", "completed", "refunded", "refund_pct", "first_day", "last_day", "lock_to_spend_min_p10", "lock_to_spend_min_p50",
                                         "lock_to_spend_min_p90", "refund_tx_pending"}, rows);
    rows.clear();
    for (auto& [k, p] : pairs) {
        bool hide = pub && p.swaps + p.refunds < 3;                    // public rules: one or two swaps' sizes must not be readable
        rows.push_back({k.first, k.second, std::to_string(p.swaps), std::to_string(p.refunds), hide ? "" : std::to_string(p.sat),
                        hide ? "" : num(quantile(p.lock, 0.5), 0), hide ? "" : num(quantile(p.lts, 0.5))});
    }
    std::sort(rows.begin(), rows.end(), [](auto& a, auto& b) { return std::stoll(a[2]) > std::stoll(b[2]); });
    write_table(out_dir + "/pairs.csv", {"pair", "direction", "swaps", "refunds", "scripted_coin_volume_base_units", "median_lock_base_units", "median_lock_to_spend_min"}, rows);
    rows.clear();
    for (auto& [k, v] : sizes) {
        std::string lo = "1e" + std::to_string(k.second), hi = "1e" + std::to_string(k.second + 1);
        rows.push_back({k.first, lo + "-" + hi, std::to_string(v[0]), std::to_string(v[1]), std::to_string(v[2])});
    }
    write_table(out_dir + "/sizes.csv", {"chain", "lock_base_units", "completed", "refunded", "offer_matched"}, rows);
    rows.clear();
    if (!pub) {
        for (auto& [a, m] : makers) {
            std::string vol;
            for (auto& [c, s] : m.sat) vol += (vol.empty() ? "" : " ") + c + "=" + std::to_string(s);
            rows.push_back({a, std::to_string(m.swaps), num(100.0 * double(m.swaps) / double(std::max<int64_t>(1, matched_total)), 2), vol, std::to_string(m.refunds)});
        }
        std::sort(rows.begin(), rows.end(), [](auto& a, auto& b) { return std::stoll(a[1]) > std::stoll(b[1]); });
        write_table(out_dir + "/makers.csv", {"maker", "swaps", "share_pct", "volume_base_units", "refunds"}, rows);
    }
    std::printf("report: %zu chain-days, %zu chains, %zu pair rows, %lld uniquely matched swaps (left out: %lld ambiguous, %lld round amount)%s -> %s\n",
                daily.size(), chains.size(), pairs.size(), (long long)matched_total, (long long)n_ambiguous, (long long)n_round, pub ? " (public rules)" : "",
                out_dir.c_str());
    return 0;
}

}  // namespace bsx
