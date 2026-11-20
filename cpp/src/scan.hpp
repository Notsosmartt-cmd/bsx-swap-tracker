// Swap recognition: script templates, tiers and scan_block (port of chainscan_lib.py; held to the corpus .events.txt goldens and the shadow database).
#pragma once
#include <functional>
#include <optional>
#include <string>
#include <vector>
#include "json.hpp"
#include "tx.hpp"

namespace bsx {

struct PrevOut {                       // a row of the rolling script-hash output index
    std::optional<int64_t> value;
    std::string otype, program;
    int64_t height = 0, time = 0;
    Json info;                         // parsed txinfo (null when absent)
};
using Lookup = std::function<std::optional<PrevOut>(const std::string& txid_hex, uint32_t vout)>;

struct P2shRow {
    std::string txid;
    uint32_t vout;
    std::optional<uint64_t> value;
    std::string otype, program;
    int64_t height, time;
    std::string txinfo;                // compact JSON
};

struct SpendRow {
    int64_t height, time;
    std::string txid;
    int vin;
    std::string script_sha256;
    int script_len;
    std::optional<std::string> script_hex;
    int nwit, nin, nout;
    std::string prev_txid;
    uint32_t prev_vout;
    std::optional<int64_t> prev_value;
};

struct Event {
    std::string kind, protocol, confidence;
    std::optional<std::string> secret_hash, secret;
    std::optional<int64_t> lock_val;
    Json extra;
    std::string txid;
    int vin = 0;
    std::string script_sha256;
    std::optional<int64_t> amount;
    std::string prev_txid;
    uint32_t prev_vout = 0;
    uint32_t sequence = 0;
    int nin = 0, nout = 0;
    int64_t version = 0;
    uint32_t locktime = 0;
    Json outputs;
    Json lock_tx;                      // null when the lock output is not in the index
    std::optional<int64_t> lock_height, lock_time;
};

struct ScanResult {
    std::vector<P2shRow> p2sh;
    std::vector<SpendRow> spends;
    std::vector<Event> events;
};

int64_t swap_tx_version(Family f);
std::string txinfo_json(const Tx& tx);
ScanResult scan_block(Family f, int64_t height, int64_t t_block, const std::vector<Tx>& txs, const Lookup& lookup, bool wide = true,
                      size_t wide_max_hex = 300, bool index_only = false);

// templates (exposed for tests)
std::optional<std::vector<Bytes>> parse_pushes(View s);
int64_t script_num(View d);

}  // namespace bsx
