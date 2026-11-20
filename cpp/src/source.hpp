// Block sources for the scan (port of chain_swap_scan.RpcSource / P2PAdapter / PartAdapter, p2p_blocks.HeaderChain / ExtendedHeaders /
// BlockPrefetcher / P2PSource, part_p2p.Headers / PartSource). Every source raises SourceUnavailable for peer, socket or header trouble (the scan
// stops that coin for this run), BlockPruned for a block the node no longer has, and ParseError for a block that cannot be parsed.
#pragma once
#include <memory>
#include <string>
#include <vector>
#include "config.hpp"
#include "net.hpp"
#include "tx.hpp"

namespace bsx {

struct SourceUnavailable : std::runtime_error { using std::runtime_error::runtime_error; };

class Source {
public:
    virtual ~Source() = default;
    virtual int64_t tip() = 0;
    virtual int64_t tip_time() = 0;
    virtual int64_t earliest(int64_t want, int64_t tip) = 0;
    virtual std::string blockhash(int64_t h) = 0;                       // display hex
    virtual Block block(int64_t h, const std::string& bh) = 0;
    virtual void close() {}
    virtual bool is_rpc() const { return false; }
    std::vector<double> lat;                                            // seconds per block request (latency tripwire for RPC sources)
};

std::unique_ptr<Source> make_source(const CoinCfg& cfg, const std::string& out_dir);

// exposed for tests
bool pow_ok(View header80, bool scrypt);
std::string header_sync_selftest(const std::string& fixture);   // "" = pass

}  // namespace bsx
