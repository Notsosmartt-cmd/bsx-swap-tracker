#include "tx.hpp"
#include <cstring>

namespace bsx {

Family family_from(const std::string& s) {
    if (s == "btc") return Family::btc;
    if (s == "ltc") return Family::ltc;
    if (s == "bch") return Family::bch;
    if (s == "part") return Family::part;
    throw std::invalid_argument("unknown family " + s);
}

const char* family_name(Family f) {
    switch (f) {
        case Family::btc: return "btc";
        case Family::ltc: return "ltc";
        case Family::bch: return "bch";
        case Family::part: return "part";
    }
    return "?";
}

size_t header_len(Family f) { return f == Family::part ? 112 : 80; }

static constexpr uint8_t PART_TX_VERSION = 0xA0;
static constexpr uint32_t PART_ANON_MARKER = 0xFFFFFFA0;

static Hash32 rev_copy(View v) {   // 32 bytes as stored (internal order)
    Hash32 h;
    std::memcpy(h.data(), v.data(), 32);
    return h;
}

static bool is_null_prev(const Hash32& h) {
    for (uint8_t b : h) if (b) return false;
    return true;
}

// Bitcoin-family transaction (segwit aware for btc and ltc, as chainscan_lib.parse_tx)
Tx parse_tx(Reader& r, Family f) {
    if (f == Family::part && r.peek() == PART_TX_VERSION) return parse_part_tx(r);
    Tx tx;
    size_t start = r.pos();
    tx.version = r.i32();
    bool segwit = false;
    if ((f == Family::btc || f == Family::ltc || f == Family::part) && r.has(2) && r.peek(0) == 0x00 && r.peek(1) != 0x00) {
        uint8_t flag = r.peek(1);
        if (flag & 1) segwit = true;
        r.seek(r.pos() + 2);
    }
    size_t body_start = r.pos();
    uint64_t n_in = r.varint();
    for (uint64_t i = 0; i < n_in; i++) {
        TxIn in;
        in.prev = rev_copy(r.read(32));
        in.vout = r.u32();
        auto ss = r.varbytes();
        in.script_sig.assign(ss.begin(), ss.end());
        in.sequence = r.u32();
        in.coinbase = is_null_prev(in.prev) && in.vout == 0xFFFFFFFFu;
        tx.vin.push_back(std::move(in));
    }
    uint64_t n_out = r.varint();
    for (uint64_t i = 0; i < n_out; i++) {
        TxOut o;
        o.value = r.u64();
        auto sc = r.varbytes();
        o.script.assign(sc.begin(), sc.end());
        tx.vout.push_back(std::move(o));
    }
    size_t body_end = r.pos();
    if (segwit) {
        for (auto& in : tx.vin) {
            uint64_t k = r.varint();
            for (uint64_t j = 0; j < k; j++) { auto w = r.varbytes(); in.witness.emplace_back(w.begin(), w.end()); }
        }
    }
    tx.locktime = r.u32();
    size_t end = r.pos();
    View raw = r.slice(start, end);
    if (segwit) {
        Bytes stripped;
        stripped.reserve(8 + body_end - body_start);
        append(stripped, raw.subspan(0, 4));
        append(stripped, r.slice(body_start, body_end));
        append(stripped, raw.subspan(raw.size() - 4, 4));
        tx.txid = sha256d(stripped);
    } else {
        tx.txid = sha256d(raw);
    }
    tx.coinbase = !tx.vin.empty() && tx.vin[0].coinbase;
    return tx;
}

// Particl transaction (version byte 0xA0): see chainscan_lib.parse_part_tx for the layout and the txid rule
Tx parse_part_tx(Reader& r) {
    Tx tx;
    size_t start = r.pos();
    uint8_t v0 = r.u8(), v1 = r.u8();
    tx.version = int64_t(v0) | (int64_t(v1) << 8);
    tx.locktime = r.u32();
    uint64_t n_in = r.varint();
    for (uint64_t i = 0; i < n_in; i++) {
        TxIn in;
        in.prev = rev_copy(r.read(32));
        in.vout = r.u32();
        auto ss = r.varbytes();
        in.script_sig.assign(ss.begin(), ss.end());
        in.sequence = r.u32();
        if (in.vout == PART_ANON_MARKER) {                 // anon (RingCT) input: scriptData stack, part of the txid serialisation
            uint64_t k = r.varint();
            for (uint64_t j = 0; j < k; j++) r.varbytes();
        }
        tx.vin.push_back(std::move(in));
    }
    Bytes stripped;
    append(stripped, r.slice(start, r.pos()));
    uint64_t n_out = r.varint();
    put_compact(stripped, n_out);
    for (uint64_t i = 0; i < n_out; i++) {
        size_t o0 = r.pos();
        uint8_t typ = r.u8();
        TxOut o;
        o.ptype = typ;
        if (typ == 1) {                                    // standard
            o.value = r.u64();
            auto sc = r.varbytes();
            o.script.assign(sc.begin(), sc.end());
            append(stripped, r.slice(o0, r.pos()));
        } else if (typ == 2) {                             // CT: commitment, data, script, range proof (stubbed in the txid)
            r.read(33); r.varbytes();
            auto sc = r.varbytes();
            o.script.assign(sc.begin(), sc.end());
            size_t keep = r.pos();
            r.varbytes();
            append(stripped, r.slice(o0, keep));
            stripped.push_back(0);
        } else if (typ == 3) {                             // RingCT: pubkey, commitment, data, range proof (stubbed)
            r.read(33); r.read(33); r.varbytes();
            size_t keep = r.pos();
            r.varbytes();
            append(stripped, r.slice(o0, keep));
            stripped.push_back(0);
        } else if (typ == 4) {                             // data
            r.varbytes();
            append(stripped, r.slice(o0, r.pos()));
        } else {
            throw ParseError("unknown Particl output type " + std::to_string(typ));
        }
        tx.vout.push_back(std::move(o));
    }
    for (auto& in : tx.vin) {
        uint64_t k = r.varint();
        for (uint64_t j = 0; j < k; j++) { auto w = r.varbytes(); in.witness.emplace_back(w.begin(), w.end()); }
    }
    tx.txid = sha256d(stripped);
    if (!tx.vin.empty() && is_null_prev(tx.vin[0].prev) && tx.vin[0].vout == 0xFFFFFFFFu) {
        tx.vin[0].coinbase = true;
        tx.coinbase = true;
    }
    return tx;
}

Block parse_block(View raw, Family f) {
    Reader r(raw);
    Block b;
    size_t hl = header_len(f);
    auto h = r.read(hl);
    b.header.assign(h.begin(), h.end());
    uint32_t t;
    std::memcpy(&t, b.header.data() + (f == Family::part ? 100 : 68), 4);
    b.time = t;
    uint64_t n = r.varint();
    if (n > raw.size()) throw ParseError("absurd transaction count");
    b.txs.reserve(size_t(n));
    for (uint64_t i = 0; i < n; i++) {
        if (f == Family::part) b.txs.push_back(r.peek() == PART_TX_VERSION ? parse_part_tx(r) : parse_tx(r, Family::btc));
        else b.txs.push_back(parse_tx(r, f));
    }
    return b;   // trailing data (Particl block signature) is ignored, as in Python
}

Hash32 merkle_root(const std::vector<Tx>& txs) {
    if (txs.empty()) return Hash32{};
    std::vector<Hash32> layer;
    layer.reserve(txs.size());
    for (auto& t : txs) layer.push_back(t.txid);
    uint8_t buf[64];
    while (layer.size() > 1) {
        if (layer.size() % 2) layer.push_back(layer.back());
        std::vector<Hash32> next;
        next.reserve(layer.size() / 2);
        for (size_t i = 0; i < layer.size(); i += 2) {
            std::memcpy(buf, layer[i].data(), 32);
            std::memcpy(buf + 32, layer[i + 1].data(), 32);
            next.push_back(sha256d(View(buf, 64)));
        }
        layer.swap(next);
    }
    return layer[0];
}

}  // namespace bsx
