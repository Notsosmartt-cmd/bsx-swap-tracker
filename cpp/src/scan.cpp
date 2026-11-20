#include "scan.hpp"
#include <algorithm>
#include <cstring>
#include "script.hpp"

namespace bsx {

static constexpr uint8_t OP_IF = 0x63, OP_ELSE = 0x67, OP_ENDIF = 0x68, OP_DROP = 0x75, OP_DUP = 0x76, OP_CSV = 0xB2, OP_CLTV = 0xB1;

int64_t swap_tx_version(Family f) { return f == Family::part ? 0xA0 : 2; }

std::optional<std::vector<Bytes>> parse_pushes(View s) {
    std::vector<Bytes> out;
    size_t i = 0, n = s.size();
    auto take = [&](size_t len) {                         // Python slice semantics: s[i:i+len] may be shorter at the end
        size_t e = std::min(n, i + len);
        out.emplace_back(s.begin() + i, s.begin() + e);
        i += len;
    };
    while (i < n) {
        uint8_t op = s[i++];
        if (op == 0x00) out.emplace_back();
        else if (op <= 0x4B) take(op);
        else if (op == 0x4C) { if (i >= n) return std::nullopt; size_t ln = s[i++]; take(ln); }
        else if (op == 0x4D) { if (i + 2 > n) return std::nullopt; size_t ln = size_t(s[i]) | (size_t(s[i + 1]) << 8); i += 2; take(ln); }
        else if (op == 0x4F) out.push_back(Bytes{0x81});
        else if (op >= 0x51 && op <= 0x60) out.push_back(Bytes{uint8_t(op - 0x50)});
        else return std::nullopt;
        if (i > n) return std::nullopt;
    }
    return out;
}

int64_t script_num(View d) {
    if (d.empty()) return 0;
    if (d.size() > 8) return 0;                            // not reached by the templates (pushes of at most 5 bytes)
    uint64_t n = 0;
    for (size_t k = 0; k < d.size(); k++) n |= uint64_t(d[k]) << (8 * k);
    if (d.back() & 0x80) {
        n &= ~(uint64_t(0x80) << (8 * (d.size() - 1)));
        return -int64_t(n);
    }
    return int64_t(n);
}

// (script, stack before it) a spend reveals; script empty = nothing revealed
static bool input_script(const TxIn& v, Bytes& script, std::vector<Bytes>& stack) {
    if (v.witness.size() >= 2) {
        script = v.witness.back();
        stack.assign(v.witness.begin(), v.witness.end() - 1);
        return true;
    }
    auto pushes = parse_pushes(v.script_sig);
    if (pushes && !pushes->empty()) {
        const Bytes& last = pushes->back();
        if ((last.size() == 33 || last.size() == 65) && (last[0] == 2 || last[0] == 3 || last[0] == 4)) return false;
        if (last.size() >= 70 && last.size() <= 73 && last[0] == 0x30) return false;
        script = last;
        stack.assign(pushes->begin(), pushes->end() - 1);
        return true;
    }
    return false;
}

static bool num_push(View s, size_t o, int64_t& val, size_t& next) {
    if (o >= s.size()) return false;
    uint8_t b = s[o];
    if (b == 0x00) { val = 0; next = o + 1; return true; }
    if (b >= 0x51 && b <= 0x60) { val = b - 0x50; next = o + 1; return true; }
    if (b >= 1 && b <= 5 && o + 1 + b <= s.size()) { val = script_num(s.subspan(o + 1, b)); next = o + 1 + b; return true; }
    return false;
}

struct Htlc { Bytes secret_hash, pkh_redeem, pkh_refund; int64_t lock_val; bool csv, sha256; };

static std::optional<Htlc> parse_htlc(View s) {
    if (s.size() < 90 || s.size() > 100) return std::nullopt;
    if (!(s[0] == OP_IF && s[1] == 0x82 && s[2] == 0x01 && s[3] == 0x20 && s[4] == 0x88 && (s[5] == 0xA8 || s[5] == 0xC0) && s[6] == 0x20)) return std::nullopt;
    if (!(s[39] == 0x88 && s[40] == OP_DUP && s[41] == 0xA9 && s[42] == 0x14 && s[63] == OP_ELSE)) return std::nullopt;
    Htlc h;
    h.secret_hash.assign(s.begin() + 7, s.begin() + 39);
    h.pkh_redeem.assign(s.begin() + 43, s.begin() + 63);
    size_t o;
    if (!num_push(s, 64, h.lock_val, o)) return std::nullopt;
    if (o + 28 != s.size() || (s[o] != OP_CSV && s[o] != OP_CLTV) || s[o + 1] != OP_DROP || s[o + 2] != OP_DUP || s[o + 3] != 0xA9 || s[o + 4] != 0x14) return std::nullopt;
    h.pkh_refund.assign(s.begin() + o + 5, s.begin() + o + 25);
    if (!(s[o + 25] == OP_ENDIF && s[o + 26] == 0x88 && s[o + 27] == 0xAC)) return std::nullopt;
    h.csv = s[o] == OP_CSV;
    h.sha256 = s[5] == 0xA8;
    return h;
}

static bool parse_adaptor_lock(View s, View& pk1, View& pk2) {
    if (s.size() == 71 && s[0] == 0x52 && s[1] == 0x21 && s[35] == 0x21 && s[69] == 0x52 && s[70] == 0xAE) {
        pk1 = s.subspan(2, 33);
        pk2 = s.subspan(36, 33);
        return true;
    }
    return false;
}

struct Refund { View pk1, pk2, pk3; int64_t csv_val; bool old = false; };

// Lock refund output script (genScriptLockRefundTxScript). BasicSwap 0.18+ (2026-08-24):
//   IF 1 CSV DROP 2 <pk> <pk> 2 CHECKMULTISIG ELSE <n> CSV DROP <pk> CHECKSIG ENDIF
// Before 0.18 the IF branch had no "1 CSV DROP" (115-byte scripts); those refunds were missed until 2026-11-20 (extra "form": "pre_0_18").
static std::optional<Refund> parse_adaptor_refund(View s) {
    size_t b;
    Refund r;
    if (s.size() >= 114 && s.size() <= 125 && s[0] == OP_IF && s[1] == 0x51 && s[2] == OP_CSV && s[3] == OP_DROP) b = 4;
    else if (s.size() >= 111 && s.size() <= 122 && s[0] == OP_IF) { b = 1; r.old = true; }
    else return std::nullopt;
    if (!(s[b] == 0x52 && s[b + 1] == 0x21 && s[b + 35] == 0x21 && s[b + 69] == 0x52 && s[b + 70] == 0xAE && s[b + 71] == OP_ELSE)) return std::nullopt;
    size_t o;
    if (!num_push(s, b + 72, r.csv_val, o)) return std::nullopt;
    if (o + 38 != s.size() || s[o] != OP_CSV || s[o + 1] != OP_DROP || s[o + 2] != 0x21 || s[o + 36] != 0xAC || s[o + 37] != OP_ENDIF) return std::nullopt;
    r.pk1 = s.subspan(b + 2, 33);
    r.pk2 = s.subspan(b + 36, 33);
    r.pk3 = s.subspan(o + 3, 33);
    return r;
}

static bool contains(View s, std::initializer_list<uint8_t> pat) {
    std::vector<uint8_t> p(pat);
    return std::search(s.begin(), s.end(), p.begin(), p.end()) != s.end();
}

static bool parse_bch_covenant(View s) {
    static const uint8_t pre[6] = {0xC3, 0x51, 0x9D, 0xC4, 0x51, 0x9D};
    return s.size() > 60 && std::memcmp(s.data(), pre, 6) == 0 && contains(s, {0xBA}) && contains(s, {OP_CSV, OP_DROP});
}

static Json jval(const std::optional<uint64_t>& v) { return v ? Json::integer(int64_t(*v)) : Json::null(); }

std::string txinfo_json(const Tx& tx) {
    Json j = Json::object();
    j.set("v", Json::integer(tx.version));
    j.set("lt", Json::integer(tx.locktime));
    j.set("nin", Json::integer(int64_t(tx.vin.size())));
    j.set("nout", Json::integer(int64_t(tx.vout.size())));
    Json ins = Json::array();
    for (size_t k = 0; k < tx.vin.size() && k < 4; k++) {
        const auto& v = tx.vin[k];
        Json e = Json::array();
        e.push(v.coinbase ? Json::null() : Json::str(hex_rev(v.prev)));
        e.push(Json::integer(v.vout));
        e.push(Json::null());                                // prevout value: not known on the raw paths
        e.push(Json::null());
        ins.push(e);
    }
    j.set("ins", ins);
    Json outs = Json::array();
    for (size_t k = 0; k < tx.vout.size() && k < 4; k++) {
        auto ot = out_type(tx.vout[k].script);
        Json e = Json::array();
        e.push(Json::str(ot.type));
        e.push(Json::str(hex(ot.program)));
        e.push(jval(tx.vout[k].value));
        outs.push(e);
    }
    j.set("outs", outs);
    return json_dump(j, true);
}

static bool is_scripthash(const char* t) { return !std::strcmp(t, "p2sh") || !std::strcmp(t, "p2wsh") || !std::strcmp(t, "p2sh32"); }

ScanResult scan_block(Family f, int64_t height, int64_t t_block, const std::vector<Tx>& txs, const Lookup& lookup, bool wide, size_t wide_max_hex, bool index_only) {
    ScanResult R;
    for (const auto& tx : txs) {
        std::string info;
        std::string txid;
        for (size_t n = 0; n < tx.vout.size(); n++) {
            auto ot = out_type(tx.vout[n].script);
            if (is_scripthash(ot.type)) {
                if (info.empty()) { info = txinfo_json(tx); txid = hex_rev(tx.txid); }
                R.p2sh.push_back({txid, uint32_t(n), tx.vout[n].value, ot.type, hex(ot.program), height, t_block, info});
            }
        }
    }
    if (index_only) return R;
    for (const auto& tx : txs) {
        if (tx.coinbase) continue;
        int nin = int(tx.vin.size()), nout = int(tx.vout.size());
        std::string txid = hex_rev(tx.txid);
        for (int i = 0; i < nin; i++) {
            const TxIn& v = tx.vin[i];
            Bytes script;
            std::vector<Bytes> stack;
            if (!input_script(v, script, stack) || script.size() < 34 || script.size() > 1000) continue;
            std::string prev_txid = hex_rev(v.prev);
            auto prev = lookup ? lookup(prev_txid, v.vout) : std::nullopt;
            std::optional<int64_t> amount;
            if (prev) amount = prev->value;
            Hash32 shh = sha256(script);
            std::string sh = hex(shh);
            if (wide)
                R.spends.push_back({height, t_block, txid, i, sh, int(script.size()), script.size() <= wide_max_hex ? std::optional<std::string>(hex(script)) : std::nullopt,
                                    int(v.witness.size()), nin, nout, prev_txid, v.vout, amount});
            std::optional<Event> ev;
            if (auto h = parse_htlc(script)) {
                const Bytes* secret = nullptr;
                for (auto& x : stack) {
                    if (x.size() == 32) { Hash32 hh = sha256(x); if (std::equal(hh.begin(), hh.end(), h->secret_hash.begin())) { secret = &x; break; } }
                }
                Event e;
                e.kind = secret ? "htlc_redeem" : ((!stack.empty() && stack.back().empty()) ? "htlc_refund" : "htlc_spend_unknown");
                e.protocol = "secret_hash";
                e.confidence = "certain";
                e.secret_hash = hex(h->secret_hash);
                if (secret) e.secret = hex(*secret);
                e.lock_val = h->lock_val;
                e.extra = Json::object();
                e.extra.set("lock_op", Json::str(h->csv ? "csv" : "cltv"));
                e.extra.set("hash_op", Json::str(h->sha256 ? "sha256" : "sha256_decred"));
                e.extra.set("pkh_redeem", Json::str(hex(h->pkh_redeem)));
                e.extra.set("pkh_refund", Json::str(hex(h->pkh_refund)));
                ev = std::move(e);
            } else if (auto a = parse_adaptor_refund(script)) {
                Event e;
                e.kind = "adaptor_refund_spend";
                e.protocol = "xmr_adaptor";
                e.confidence = "certain";
                e.lock_val = a->csv_val;
                e.extra = Json::object();
                e.extra.set("branch", Json::str((stack.size() >= 3 && stack[0].empty()) ? "lock_refund_complete" : "timeout"));
                e.extra.set("pk1", Json::str(hex(a->pk1)));
                e.extra.set("pk2", Json::str(hex(a->pk2)));
                e.extra.set("pk3", Json::str(hex(a->pk3)));
                if (a->old) e.extra.set("form", Json::str("pre_0_18"));
                ev = std::move(e);
            } else {
                View pk1, pk2;
                if (parse_adaptor_lock(script, pk1, pk2)) {
                    std::vector<std::string> outs;
                    for (auto& o : tx.vout) outs.emplace_back(out_type(o.script).type);
                    bool csv_seq = (v.sequence & 0x80000000u) == 0 && v.sequence != 0;
                    Event e;
                    e.protocol = "xmr_adaptor";
                    e.extra = Json::object();
                    if (nin == 1 && nout == 1 && (outs[0] == "p2wsh" || outs[0] == "p2sh") && csv_seq) {
                        e.kind = "adaptor_refund_tx";
                        e.confidence = "likely";
                        e.lock_val = v.sequence & 0xFFFF;
                        e.extra.set("pk1", Json::str(hex(pk1)));
                        e.extra.set("pk2", Json::str(hex(pk2)));
                    } else {
                        Json lock_lt;                                   // null = unknown
                        if (prev && prev->info.t == Json::T::Obj) { if (const Json* lt = prev->info.get("lt")) lock_lt = *lt; }
                        bool lt_ok = lock_lt.is_null() || (lock_lt.t == Json::T::Int && lock_lt.i == 0);
                        // strong needs lock nLockTime 0; a height-based lock time was tested 2026-11-20 and not adopted (see chainscan_lib.py)
                        bool plain = nin == 1 && nout == 1 && (outs[0] == "p2pkh" || outs[0] == "p2wpkh") && tx.version == swap_tx_version(f);
                        bool seq0 = v.sequence == 0;
                        e.kind = "adaptor_lock_spend";
                        e.confidence = (plain && seq0 && lt_ok) ? "strong" : ((plain && lt_ok) ? "likely" : "candidate");
                        e.extra.set("pk1", Json::str(hex(pk1)));
                        e.extra.set("pk2", Json::str(hex(pk2)));
                        e.extra.set("plain_spend", Json::boolean(plain));
                        e.extra.set("seq0", Json::boolean(seq0));
                        e.extra.set("lock_nlocktime", lock_lt);
                        e.extra.set("lock_age_blocks", prev ? Json::integer(height - prev->height) : Json::null());
                    }
                    ev = std::move(e);
                } else if (f == Family::bch && parse_bch_covenant(script)) {
                    Event e;
                    e.kind = "bch_covenant_spend";
                    e.protocol = "xmr_bch_covenant";
                    e.confidence = "certain";
                    e.extra = Json::object();
                    e.extra.set("len", Json::integer(int64_t(script.size())));
                    ev = std::move(e);
                }
            }
            if (ev) {
                Event& e = *ev;
                e.txid = txid;
                e.vin = i;
                e.script_sha256 = sh;
                e.amount = amount;
                e.prev_txid = prev_txid;
                e.prev_vout = v.vout;
                e.sequence = v.sequence;
                e.nin = nin;
                e.nout = nout;
                e.version = tx.version;
                e.locktime = tx.locktime;
                e.outputs = Json::array();
                for (auto& o : tx.vout) {
                    auto ot = out_type(o.script);
                    Json r = Json::array();
                    r.push(Json::str(ot.type));
                    r.push(Json::str(hex(ot.program)));
                    r.push(jval(o.value));
                    e.outputs.push(r);
                }
                if (prev) { e.lock_tx = prev->info; e.lock_height = prev->height; e.lock_time = prev->time; }
                R.events.push_back(std::move(e));
            }
        }
    }
    return R;
}

}  // namespace bsx
