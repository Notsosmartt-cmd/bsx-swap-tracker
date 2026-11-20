// Transactions and blocks for the families the scan reads: btc (segwit), ltc (segwit; MWEB as the Python parser treats it), bch, part (Particl).
// Port of python/collectors/chainscan_lib.py parse_tx / parse_part_tx / parse_raw_block; held to byte-identical output by the golden corpus.
#pragma once
#include <optional>
#include <string>
#include <vector>
#include "bytes.hpp"
#include "hash.hpp"

namespace bsx {

enum class Family { btc, ltc, bch, part };
Family family_from(const std::string& s);
const char* family_name(Family f);
size_t header_len(Family f);   // 80, or 112 for Particl

struct TxIn {
    Hash32 prev{};                 // internal byte order
    bool coinbase = false;
    uint32_t vout = 0;
    Bytes script_sig;
    uint32_t sequence = 0;
    std::vector<Bytes> witness;
};

struct TxOut {
    std::optional<uint64_t> value;  // empty for Particl CT / RingCT / data outputs (hidden or none)
    Bytes script;
    int ptype = 1;                  // Particl output type (1 standard), 1 for other families
};

struct Tx {
    Hash32 txid{};                  // internal byte order
    int64_t version = 0;
    uint32_t locktime = 0;
    bool coinbase = false;
    std::vector<TxIn> vin;
    std::vector<TxOut> vout;
};

struct Block {
    Bytes header;
    uint32_t time = 0;
    std::vector<Tx> txs;
};

Tx parse_tx(Reader& r, Family f);
Tx parse_part_tx(Reader& r);
Block parse_block(View raw, Family f);
Hash32 merkle_root(const std::vector<Tx>& txs);

}  // namespace bsx
