// Coin table and fixed safety constants (port of chain_swap_scan.COINS / p2p_blocks.NETS / part_p2p.NET).
#pragma once
#include <cstdint>
#include <set>
#include <string>
#include <vector>
#include "tx.hpp"

namespace bsx {

enum class Mode { rpc_raw, p2p, part_p2p };

struct CoinCfg {
    std::string coin;
    Family family;
    Mode mode;
    int bpd;                       // blocks per day
    double rps;                    // throttle cap
    // RPC coins
    int rpc_port = 0;
    std::string cred_ref;          // chainclients key in coindata/basicswap.json
    // P2P coins
    uint32_t magic = 0;            // as the 4 bytes appear on the wire, read little-endian
    int p2p_port = 0;
    int protocol = 70015;
    bool scrypt = false;
    int workers = 4, batch = 8;
    size_t max_bytes = 48u << 20;
    std::vector<std::string> seeds;
    std::string headers;           // Electrum header file, relative to settings().electrum_headers_dir
    std::string genesis;           // hex, display order
    // built-in header sync (used when no Electrum header file is present): trusted anchor, synced forward from here with every header's
    // link, proof of work and a difficulty floor (target at most 4x the checkpoint's) checked
    int64_t checkpoint_height = 0;
    std::string checkpoint_hash;   // display hex
    uint32_t checkpoint_bits = 0;
    int depth_days = 0;            // 0 = as far as the source allows
    int p2sh_retain_days = 21;
    size_t wide_max_hex = 300;
};

const CoinCfg& coin_cfg(const std::string& coin);   // throws for unknown coins
std::vector<std::string> default_coins();          // BTC, LTC, BCH, PART

// protected ports (never contacted): settings().protected_ports (BasicSwap's defaults plus the host's [safety] list), see settings.hpp

constexpr int REORG_DEPTH = 8;
constexpr double WARMUP_DAYS = 2;
constexpr int SYNCED_MAX_AGE_S = 36 * 3600;
constexpr double FIRST_SCAN_DAYS = 7;   // first scan without --backfill-days
constexpr double MAX_LAG_H = 3.0;

}  // namespace bsx
