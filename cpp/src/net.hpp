// TCP helpers, the allowlisted JSON-RPC client (port of chain_swap_scan.ChainRPC) and the peer-to-peer client (port of p2p_blocks.Peer and
// part_p2p.LocalPeer). Reading only: the RPC client refuses every method outside the four-method allowlist BEFORE connecting, talks to loopback
// only and never to a protected port; the P2P handshake announces no services and serves nothing.
#pragma once
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include "bytes.hpp"
#include "config.hpp"
#include "hash.hpp"
#include "json.hpp"

namespace bsx {

struct NetError : std::runtime_error { using std::runtime_error::runtime_error; };        // peer or socket trouble (Python P2PError / OSError)
struct RpcNotAllowed : std::runtime_error { using std::runtime_error::runtime_error; };
struct RpcUnavailable : std::runtime_error { using std::runtime_error::runtime_error; };
struct BlockPruned : std::runtime_error { using std::runtime_error::runtime_error; };

class Socket {
public:
    Socket() = default;
    Socket(const std::string& ip, int port, double timeout);
    ~Socket();
    Socket(Socket&& o) noexcept : fd_(o.fd_) { o.fd_ = -1; }
    Socket& operator=(Socket&& o) noexcept { close(); fd_ = o.fd_; o.fd_ = -1; return *this; }
    void send_all(const uint8_t* p, size_t n, double timeout);
    long recv_some(uint8_t* p, size_t n, double timeout);   // 0 = closed, -1 = timeout
    void close();
    bool open() const { return fd_ >= 0; }

private:
    int fd_ = -1;
};

std::vector<std::string> resolve_ipv4(const std::string& host, int port);

class ChainRPC {
public:
    ChainRPC(const std::string& coin, const std::string& host, int port, const std::string& user, const std::string& password, double timeout = 180);
    static void check(const std::string& method, const Json& params);   // throws RpcNotAllowed
    Json call(const std::string& method, const Json& params = Json::array());
    std::vector<double> lat;

private:
    std::string coin_, host_, auth_;
    int port_;
    double timeout_;
};

// credentials from coindata/basicswap.json chainclients[cfg.cred_ref]; never logged; refuses non-loopback hosts and unexpected or protected ports
ChainRPC make_rpc(const CoinCfg& cfg, double timeout = 180);

constexpr uint32_t MSG_TX = 1, MSG_BLOCK = 2, MSG_WITNESS_TX = 0x40000001, MSG_WITNESS_BLOCK = 0x40000002;

class Peer {
public:
    Peer(const CoinCfg& cfg, const std::string& ip, int port, double timeout, bool relay = false);
    void send(const std::string& cmd, const Bytes& payload = {});
    bool recv(std::string& cmd, Bytes& payload, double timeout);        // false = nothing complete within timeout; partial data stays buffered
    std::map<std::string, Bytes> get_blocks(const std::vector<Hash32>& hashes);   // hash (raw bytes as string key) -> raw block
    std::vector<Bytes> getheaders(const std::vector<Hash32>& locator, size_t header_len);
    const std::string& ip() const { return ip_; }
    int start_height = -1;
    uint64_t services = 0;
    void close() { sock_.close(); }

private:
    const CoinCfg& cfg_;
    std::string ip_;
    double timeout_;
    Socket sock_;
    Bytes buf_;
};

Bytes version_payload(int protocol, bool relay);
// One complete P2P message from the front of buf (magic, length cap 64 MB, checksum); false = incomplete (buf untouched). Throws NetError on
// garbage. Pure, so it can be fuzzed (fuzz/fuzz_frame.cpp).
bool frame_next(Bytes& buf, uint32_t magic, std::string& cmd, Bytes& payload);
std::string hash_key(const Hash32& h);

}  // namespace bsx
