// `bsx-swap-tracker snapshot ...`: board snapshots for the offer match from your OWN BasicSwap node (2026-11-20). Reads the node's offer list
// (POST /json/offers with a fixed read-only filter, the call the node's own offers page makes) and writes <snapshots_dir>/<UTC>Z.json.gz in the
// format the matcher reads ({"timestamp", "source", "offers": [...]}, one offer per entry, fields as the public markets site names them).
//
// Safety: this is the ONE place the program talks to a BasicSwap UI port (which the RPC client refuses as protected). It sends one request
// shape only - POST /json/offers with {"limit", "sort_by", "sort_dir", "with_extra_info"} - to a loopback address named explicitly with --url
// (there is no default URL). It never calls any other path, never sends credentials, and never posts anything that changes state.
//
// Conversion from the node's fields: msg_id = offer_id, timestamp = created_at, coin names -> tickers, decimal amount strings -> integer base
// units (12 decimals XMR, 11 WOW, 8 otherwise), time_valid = expire_at - created_at, fee rates from the extra info. Not available from the node
// and left out (the matcher then leaves them empty): lock_type, lock_value, protocol_version. "rate" is the node's rate (coin_to per coin_from).
//   bsx-swap-tracker [--config F] snapshot --url http://127.0.0.1:PORT [--snapshots-dir DIR] [--interval S] [--count N]
//   bsx-swap-tracker snapshot --from-jsonl FILE.jsonl[.gz] [--snapshots-dir DIR]   (convert recorded node offer lists, one {"ts", "offers"} per line)
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <map>
#include <optional>
#include <thread>
#include <zlib.h>
#include "json.hpp"
#include "net.hpp"
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

namespace {

const std::map<std::string, std::string> TICKER = {
    {"Bitcoin", "BTC"}, {"Litecoin", "LTC"}, {"Particl", "PART"}, {"Particl Anon", "PART_ANON"}, {"Particl Blind", "PART_BLIND"},
    {"Monero", "XMR"}, {"Wownero", "WOW"}, {"Bitcoin Cash", "BCH"}, {"Dash", "DASH"}, {"Firo", "FIRO"}, {"PIVX", "PIVX"},
    {"Dogecoin", "DOGE"}, {"Decred", "DCR"}, {"Namecoin", "NMC"}, {"Litecoin MWEB", "LTC_MWEB"}};

int decimals(const std::string& ticker) { return ticker == "XMR" ? 12 : ticker == "WOW" ? 11 : 8; }

std::optional<int64_t> base_units(const std::string& s, int dec) {          // "0.01500000" -> 1500000 (exact, no floating point)
    size_t dot = s.find('.');
    std::string ip = s.substr(0, dot), fp = dot == std::string::npos ? "" : s.substr(dot + 1);
    if (ip.empty() || ip.find_first_not_of("0123456789") != std::string::npos || fp.find_first_not_of("0123456789") != std::string::npos) return std::nullopt;
    if (int(fp.size()) > dec) { if (fp.find_first_not_of('0', size_t(dec)) != std::string::npos) return std::nullopt; fp.resize(size_t(dec)); }
    fp.append(size_t(dec) - fp.size(), '0');
    try { return std::stoll(ip + fp); } catch (...) { return std::nullopt; }
}

const Json* field(const Json& o, const char* k) { const Json* v = o.get(k); return v && !v->is_null() ? v : nullptr; }

Json convert(const Json& o) {                                    // one node offer -> markets-site offer (null on anything unusable)
    const Json *id = field(o, "offer_id"), *cf = field(o, "coin_from"), *ct = field(o, "coin_to");
    if (!id || id->t != Json::T::Str || !cf || cf->t != Json::T::Str || !ct || ct->t != Json::T::Str) return Json::null();
    auto tf = TICKER.find(cf->s), tt = TICKER.find(ct->s);
    std::string tkf = tf == TICKER.end() ? "?" + cf->s : tf->second, tkt = tt == TICKER.end() ? "?" + ct->s : tt->second;
    Json r = Json::object();
    r.set("msg_id", Json::str(id->s));
    r.set("coin_from", Json::str(tkf));
    r.set("coin_to", Json::str(tkt));
    auto amount = [&](const char* src, const char* dst, const std::string& tk) {
        const Json* v = field(o, src);
        if (!v || v->t != Json::T::Str) return;
        r.set((std::string(dst) + "_str").c_str(), Json::str(v->s));
        if (auto u = base_units(v->s, decimals(tk))) r.set(dst, Json::integer(*u));
    };
    amount("amount_from", "amount_from", tkf);
    amount("amount_to", "amount_to", tkt);
    amount("min_bid_amount", "min_bid_amount", tkf);
    if (const Json* v = field(o, "rate"); v && v->t == Json::T::Str) { try { r.set("rate", Json::number(std::stod(v->s))); } catch (...) {} }
    const Json *c = field(o, "created_at"), *e = field(o, "expire_at");
    if (c && c->t == Json::T::Int) r.set("timestamp", Json::integer(c->i));
    if (c && e && c->t == Json::T::Int && e->t == Json::T::Int) r.set("time_valid", Json::integer(e->i - c->i));
    for (auto [src, dst] : {std::pair{"addr_from", "addr_from"}, {"auto_accept_type", "auto_accept_type"}, {"feerate_from", "fee_rate_from"},
                            {"feerate_to", "fee_rate_to"}, {"swap_type", "swap_type"}, {"amount_negotiable", "amount_negotiable"},
                            {"rate_negotiable", "rate_negotiable"}, {"message_nets", "message_nets"}})
        if (const Json* v = field(o, src)) r.set(dst, *v);
    if (const Json* v = field(o, "is_expired"); v && v->t == Json::T::Bool && v->b) return Json::null();
    if (const Json* v = field(o, "is_revoked"); v && v->t == Json::T::Bool && v->b) return Json::null();
    return r;
}

std::string stamp(int64_t ts) {
    char b[32];
    time_t t = time_t(ts);
    struct tm tm{};
    gmtime_r(&t, &tm);
    strftime(b, sizeof b, "%Y%m%dT%H%M%SZ", &tm);
    return b;
}

bool write_snapshot(const std::string& dir, int64_t ts, const Json& node_offers, size_t& n_out) {
    Json snap = Json::object(), arr = Json::array();
    snap.set("timestamp", Json::integer(ts));
    snap.set("source", Json::str("basicswap-node"));
    if (node_offers.t == Json::T::Arr)
        for (auto& o : *node_offers.a) { Json c = convert(o); if (!c.is_null()) arr.push(std::move(c)); }
    n_out = arr.a->size();
    snap.set("offers", arr);
    std::string text = json_dump(snap), path = dir + "/" + stamp(ts) + ".json.gz", tmp = dir + "/.tmp_" + stamp(ts) + ".json.gz";
    make_dirs(dir);
    gzFile g = gzopen(tmp.c_str(), "wb6");
    if (!g) return false;
    bool ok = gzwrite(g, text.data(), unsigned(text.size())) == int(text.size());
    ok = (gzclose(g) == Z_OK) && ok;
    if (!ok || std::rename(tmp.c_str(), path.c_str()) != 0) { std::remove(tmp.c_str()); return false; }
    return true;
}

struct Url { std::string host; int port; };

Url parse_url(const std::string& u) {
    const std::string pre = "http://";
    if (u.compare(0, pre.size(), pre) != 0) throw std::invalid_argument("--url must be http://<loopback>:<port>");
    std::string rest = u.substr(pre.size());
    while (!rest.empty() && rest.back() == '/') rest.pop_back();
    size_t c = rest.rfind(':');
    if (c == std::string::npos || rest.find('/') != std::string::npos) throw std::invalid_argument("--url must be http://<loopback>:<port> (no path)");
    std::string host = rest.substr(0, c);
    if (host == "localhost") host = "127.0.0.1";
    if (host.compare(0, 4, "127.") != 0) throw std::invalid_argument("--url must name a loopback address (127.x.x.x or localhost): your own node only");
    int port = std::stoi(rest.substr(c + 1));
    if (port <= 0 || port > 65535) throw std::invalid_argument("bad port in --url");
    return {host, port};
}

Json fetch_offers(const Url& u) {
    const std::string body = R"({"limit": 5000, "sort_by": "created_at", "sort_dir": "desc", "with_extra_info": true})";   // fixed, read-only filter
    std::string req = "POST /json/offers HTTP/1.1\r\nHost: " + u.host + ":" + std::to_string(u.port) +
                      "\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + std::to_string(body.size()) + "\r\n\r\n" + body;
    std::string resp;
    Socket s(u.host, u.port, 10);
    s.send_all(reinterpret_cast<const uint8_t*>(req.data()), req.size(), 30);
    uint8_t buf[65536];
    double end = now_s() + 60;
    while (true) {
        double left = end - now_s();
        if (left <= 0) throw NetError("timeout reading the node's offers");
        long r = s.recv_some(buf, sizeof buf, std::min(left, 15.0));
        if (r == 0) break;
        if (r > 0) resp.append(reinterpret_cast<char*>(buf), size_t(r));
        if (resp.size() > (64u << 20)) throw NetError("offer list larger than 64 MB");
    }
    size_t he = resp.find("\r\n\r\n");
    if (he == std::string::npos || (resp.compare(0, 9, "HTTP/1.0 ") != 0 && resp.compare(0, 9, "HTTP/1.1 ") != 0)) throw NetError("not an HTTP response");
    if (resp.compare(9, 3, "200") != 0) throw NetError("node answered " + resp.substr(9, 3));
    std::string head = resp.substr(0, he), payload = resp.substr(he + 4);
    std::string lower = head;
    std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
    if (lower.find("transfer-encoding: chunked") != std::string::npos) {
        std::string out;
        for (size_t p = 0; p < payload.size();) {
            size_t le = payload.find("\r\n", p);
            if (le == std::string::npos) break;
            size_t n = std::stoul(payload.substr(p, le - p), nullptr, 16);
            if (n == 0) break;
            out += payload.substr(le + 2, n);
            p = le + 2 + n + 2;
        }
        payload = out;
    }
    Json j = json_parse(payload);
    if (j.t != Json::T::Arr) throw NetError("node's offer list is not a JSON array");
    return j;
}

std::string read_maybe_gz(const std::string& path) {
    gzFile g = gzopen(path.c_str(), "rb");                          // reads plain files too (zlib passes them through)
    if (!g) throw std::runtime_error("cannot open " + path);
    std::string out;
    char buf[65536];
    int r;
    while ((r = gzread(g, buf, sizeof buf)) > 0) out.append(buf, size_t(r));
    gzclose(g);
    if (r < 0) throw std::runtime_error("cannot read " + path);
    return out;
}

}  // namespace

int snapshot_main(int argc, char** argv) {
    std::string url, from;
    double interval = 0;
    long count = 1;
    for (int i = 2; i < argc; i++) {
        std::string k = argv[i];
        auto val = [&]() -> std::string { if (i + 1 >= argc) throw std::invalid_argument(k + " needs a value"); return argv[++i]; };
        if (k == "--url") url = val();
        else if (k == "--from-jsonl") from = val();
        else if (k == "--snapshots-dir") override_snapshots_dir(val());
        else if (k == "--interval") interval = std::stod(val());
        else if (k == "--count") count = std::stol(val());
        else throw std::invalid_argument("unknown flag " + k);
    }
    const std::string dir = settings().snapshots_dir;
    if (!from.empty()) {
        std::string text = read_maybe_gz(from);
        size_t files = 0, offers = 0, bad = 0, pos = 0;
        while (pos < text.size()) {
            size_t e = text.find('\n', pos);
            std::string line = text.substr(pos, e == std::string::npos ? std::string::npos : e - pos);
            pos = e == std::string::npos ? text.size() : e + 1;
            if (line.empty()) continue;
            try {
                Json j = json_parse(line);
                const Json *ts = j.get("ts"), *of = j.get("offers");
                if (!ts || ts->t != Json::T::Int || !of) { bad++; continue; }
                size_t n;
                if (!write_snapshot(dir, ts->i, *of, n)) { fprintf(stderr, "snapshot: cannot write into %s\n", dir.c_str()); return 1; }
                files++; offers += n;
            } catch (...) { bad++; }
        }
        printf("snapshot: %zu snapshot files (%zu offers) written to %s from %s; %zu unreadable lines\n", files, offers, dir.c_str(), from.c_str(), bad);
        return bad && !files ? 1 : 0;
    }
    if (url.empty()) { fprintf(stderr, "snapshot: --url http://127.0.0.1:PORT (your own BasicSwap node) or --from-jsonl FILE is required; there is no default\n"); return 2; }
    Url u = parse_url(url);
    if (interval > 0 && interval < 15) throw std::invalid_argument("--interval below 15 s is not allowed (the node is not a high-rate API)");
    for (long k = 0; count <= 0 || k < count; k++) {
        if (k) std::this_thread::sleep_for(std::chrono::milliseconds(int64_t(interval * 1000)));
        try {
            Json offers = fetch_offers(u);
            size_t n;
            int64_t ts = now_i();
            if (!write_snapshot(dir, ts, offers, n)) { fprintf(stderr, "snapshot: cannot write into %s\n", dir.c_str()); return 1; }
            printf("snapshot: %s %zu of %zu node offers -> %s/%s.json.gz\n", utc_iso(ts).c_str(), n, offers.a->size(), dir.c_str(), stamp(ts).c_str());
        } catch (const std::exception& e) {
            fprintf(stderr, "snapshot: %s\n", e.what());
            if (interval <= 0) return 1;
        }
        fflush(stdout);
        if (interval <= 0) break;
    }
    return 0;
}

}  // namespace bsx
