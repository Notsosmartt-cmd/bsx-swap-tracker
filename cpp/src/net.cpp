#include "net.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <random>
#include <regex>
#include <sys/socket.h>
#include <unistd.h>
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

// ------------------------------------------------------------------ sockets

Socket::Socket(const std::string& ip, int port, double timeout) {
    fd_ = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd_ < 0) throw NetError("socket");
    int fl = fcntl(fd_, F_GETFL, 0);
    fcntl(fd_, F_SETFL, fl | O_NONBLOCK);
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(uint16_t(port));
    if (inet_pton(AF_INET, ip.c_str(), &a.sin_addr) != 1) { close(); throw NetError("bad address " + ip); }
    int r = ::connect(fd_, reinterpret_cast<sockaddr*>(&a), sizeof a);
    if (r != 0 && errno != EINPROGRESS) { close(); throw NetError("connect " + ip + ": " + strerror(errno)); }
    if (r != 0) {
        pollfd p{fd_, POLLOUT, 0};
        int pr = poll(&p, 1, int(timeout * 1000));
        int err = 0;
        socklen_t el = sizeof err;
        if (pr <= 0 || getsockopt(fd_, SOL_SOCKET, SO_ERROR, &err, &el) != 0 || err != 0) { close(); throw NetError("connect " + ip + " timed out or refused"); }
    }
    int one = 1;
    setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
}

Socket::~Socket() { close(); }

void Socket::close() {
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
}

void Socket::send_all(const uint8_t* p, size_t n, double timeout) {
    double end = now_s() + timeout;
    while (n) {
        ssize_t w = ::send(fd_, p, n, MSG_NOSIGNAL);
        if (w > 0) { p += w; n -= size_t(w); continue; }
        if (w < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) throw NetError(std::string("send: ") + strerror(errno));
        double left = end - now_s();
        if (left <= 0) throw NetError("send timed out");
        pollfd pf{fd_, POLLOUT, 0};
        poll(&pf, 1, int(left * 1000) + 1);
    }
}

long Socket::recv_some(uint8_t* p, size_t n, double timeout) {
    pollfd pf{fd_, POLLIN, 0};
    int pr = poll(&pf, 1, int(timeout * 1000));
    if (pr == 0) return -1;
    if (pr < 0) { if (errno == EINTR) return -1; throw NetError(std::string("poll: ") + strerror(errno)); }
    ssize_t r = ::recv(fd_, p, n, 0);
    if (r < 0) { if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return -1; throw NetError(std::string("recv: ") + strerror(errno)); }
    return long(r);
}

std::vector<std::string> resolve_ipv4(const std::string& host, int port) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    std::vector<std::string> out;
    if (getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &res) != 0) return out;
    for (addrinfo* a = res; a; a = a->ai_next) {
        char b[64];
        inet_ntop(AF_INET, &reinterpret_cast<sockaddr_in*>(a->ai_addr)->sin_addr, b, sizeof b);
        if (std::find(out.begin(), out.end(), b) == out.end()) out.push_back(b);
    }
    freeaddrinfo(res);
    return out;
}

// ------------------------------------------------------------------ JSON-RPC (allowlisted)

static std::string b64(const std::string& s) {
    static const char* t = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string o;
    size_t i = 0;
    while (i + 2 < s.size()) {
        uint32_t v = (uint8_t(s[i]) << 16) | (uint8_t(s[i + 1]) << 8) | uint8_t(s[i + 2]);
        o += t[v >> 18]; o += t[(v >> 12) & 63]; o += t[(v >> 6) & 63]; o += t[v & 63];
        i += 3;
    }
    if (i + 1 == s.size()) { uint32_t v = uint8_t(s[i]) << 16; o += t[v >> 18]; o += t[(v >> 12) & 63]; o += "=="; }
    else if (i + 2 == s.size()) { uint32_t v = (uint8_t(s[i]) << 16) | (uint8_t(s[i + 1]) << 8); o += t[v >> 18]; o += t[(v >> 12) & 63]; o += t[(v >> 6) & 63]; o += '='; }
    return o;
}

ChainRPC::ChainRPC(const std::string& coin, const std::string& host, int port, const std::string& user, const std::string& password, double timeout)
    : coin_(coin), host_(host), auth_("Basic " + b64(user + ":" + password)), port_(port), timeout_(timeout) {}

void ChainRPC::check(const std::string& method, const Json& params) {
    static const std::set<std::string> allowed = {"getblockcount", "getblockhash", "getbestblockhash", "getblock"};
    if (!allowed.count(method)) throw RpcNotAllowed("method '" + method + "' is not on the allowlist");
    size_t n = params.t == Json::T::Arr ? params.a->size() : 0;
    if (method == "getblock") {
        static const std::regex hex64("^[0-9a-f]{64}$");
        bool ok = (n == 2 || n == 3) && (*params.a)[0].t == Json::T::Str && std::regex_match((*params.a)[0].s, hex64);
        if (ok) {
            const Json& v = (*params.a)[1];
            ok = (v.t == Json::T::Int && v.i >= 0 && v.i <= 3) || v.t == Json::T::Bool;
        }
        if (ok && n == 3) ok = (*params.a)[2].t == Json::T::Bool && (*params.a)[2].b;
        if (!ok) throw RpcNotAllowed("getblock parameters outside the allowed shapes");
    } else if (method == "getblockhash") {
        if (!(n == 1 && (*params.a)[0].t == Json::T::Int && (*params.a)[0].i >= 0)) throw RpcNotAllowed("getblockhash parameter");
    } else if (n) {
        throw RpcNotAllowed(method + " takes no parameters");
    }
}

Json ChainRPC::call(const std::string& method, const Json& params) {
    check(method, params);
    Json body = Json::object();
    body.set("jsonrpc", Json::str("1.0"));
    body.set("id", Json::str("chainscan"));
    body.set("method", Json::str(method));
    body.set("params", params);
    std::string b = json_dump(body);
    std::string req = "POST / HTTP/1.1\r\nHost: " + host_ + "\r\nContent-Type: application/json\r\nAuthorization: " + auth_ +
                      "\r\nConnection: close\r\nContent-Length: " + std::to_string(b.size()) + "\r\n\r\n" + b;   // root path only, never /wallet/...
    double t0 = now_s();
    std::string resp;
    try {
        Socket s(host_, port_, 10);
        s.send_all(reinterpret_cast<const uint8_t*>(req.data()), req.size(), timeout_);
        uint8_t buf[65536];
        double end = now_s() + timeout_;
        while (true) {
            double left = end - now_s();
            if (left <= 0) throw NetError("timeout");
            long r = s.recv_some(buf, sizeof buf, std::min(left, 15.0));
            if (r == 0) break;
            if (r > 0) resp.append(reinterpret_cast<char*>(buf), size_t(r));
        }
    } catch (const NetError& e) {
        throw RpcUnavailable(coin_ + ": " + e.what());
    }
    lat.push_back(now_s() - t0);
    if (lat.size() > 50) lat.erase(lat.begin(), lat.end() - 50);
    size_t he = resp.find("\r\n\r\n");
    if (he == std::string::npos) throw RpcUnavailable(coin_ + ": unreadable response");
    std::string head = resp.substr(0, he), payload = resp.substr(he + 4);
    if (head.find("Transfer-Encoding: chunked") != std::string::npos || head.find("transfer-encoding: chunked") != std::string::npos) {
        std::string out;
        size_t p = 0;
        while (p < payload.size()) {
            size_t le = payload.find("\r\n", p);
            if (le == std::string::npos) break;
            size_t n = std::stoul(payload.substr(p, le - p), nullptr, 16);
            if (n == 0) break;
            out += payload.substr(le + 2, n);
            p = le + 2 + n + 2;
        }
        payload = out;
    }
    Json msg;
    try { msg = json_parse(payload); } catch (const std::exception&) { throw RpcUnavailable(coin_ + ": unreadable response"); }
    const Json* err = msg.get("error");
    if (err && !err->is_null()) {
        std::string text = err->t == Json::T::Obj && err->get("message") ? err->get("message")->as_str() : json_dump(*err);
        std::string low = text;
        for (auto& c : low) c = char(tolower(c));
        if (low.find("pruned") != std::string::npos || low.find("undo data") != std::string::npos || low.find("not available") != std::string::npos) throw BlockPruned(text);
        if (err->t == Json::T::Obj && err->get("code") && err->get("code")->as_int() == -28) throw RpcUnavailable(coin_ + ": node is starting (" + text + ")");
        throw RpcUnavailable(coin_ + ": RPC error " + text.substr(0, 80));
    }
    const Json* r = msg.get("result");
    return r ? *r : Json();
}

ChainRPC make_rpc(const CoinCfg& cfg, double timeout) {
    Json conf = json_parse(read_text(settings().basicswap_json));
    const Json* cc = conf.get("chainclients");
    const Json* c = cc ? cc->get(cfg.cred_ref) : nullptr;
    if (!c) throw RpcNotAllowed(cfg.coin + ": no chainclients entry");
    std::string host = c->get("rpchost") ? c->get("rpchost")->as_str() : "127.0.0.1";
    int port = c->get("rpcport") ? int(c->get("rpcport")->as_int()) : 0;
    if (host != "127.0.0.1" && host != "localhost") throw RpcNotAllowed(cfg.coin + ": RPC host is not loopback");
    if (port != cfg.rpc_port || settings().protected_ports.count(port)) throw RpcNotAllowed(cfg.coin + ": configured RPC port does not match the expected chain-daemon port");
    const Json* u = c->get("rpcuser");
    const Json* p = c->get("rpcpassword");
    if (!u || !p) throw RpcNotAllowed(cfg.coin + ": no RPC credentials");
    return ChainRPC(cfg.coin, "127.0.0.1", port, u->as_str(), p->as_str(), timeout);
}

// ------------------------------------------------------------------ peer to peer

bool frame_next(Bytes& buf, uint32_t magic, std::string& cmd, Bytes& payload) {
    if (buf.size() < 24) return false;
    uint32_t m;
    std::memcpy(&m, buf.data(), 4);
    if (m != magic) throw NetError("wrong network magic");
    uint32_t ln;
    std::memcpy(&ln, buf.data() + 16, 4);
    if (ln > 64u * 1024 * 1024) throw NetError("oversized message");
    if (buf.size() < 24 + size_t(ln)) return false;
    payload.assign(buf.begin() + 24, buf.begin() + 24 + ln);
    Hash32 cs = sha256d(payload);
    if (std::memcmp(cs.data(), buf.data() + 20, 4) != 0) throw NetError("bad checksum");
    cmd.assign(reinterpret_cast<const char*>(buf.data() + 4), strnlen(reinterpret_cast<const char*>(buf.data() + 4), 12));
    buf.erase(buf.begin(), buf.begin() + 24 + ln);
    return true;
}

std::string hash_key(const Hash32& h) { return std::string(reinterpret_cast<const char*>(h.data()), 32); }

Bytes version_payload(int protocol, bool relay) {
    Bytes p;
    auto le = [&](uint64_t v, int n) { for (int k = 0; k < n; k++) p.push_back(uint8_t(v >> (8 * k))); };
    le(uint32_t(protocol), 4);
    le(0, 8);                                                   // services: none
    le(uint64_t(now_i()), 8);
    for (int k = 0; k < 2; k++) {                               // addr_recv, addr_from: no services, ::ffff:0.0.0.0, port 0
        le(0, 8);
        for (int z = 0; z < 10; z++) p.push_back(0);
        p.push_back(0xFF); p.push_back(0xFF);
        for (int z = 0; z < 4; z++) p.push_back(0);
        p.push_back(0); p.push_back(0);
    }
    static std::mt19937_64 rng(std::random_device{}());
    le(rng(), 8);
    std::string ua = "/chainscan-cpp:0.3/";
    p.push_back(uint8_t(ua.size()));
    p.insert(p.end(), ua.begin(), ua.end());
    le(0, 4);                                                   // start height
    p.push_back(relay ? 1 : 0);
    return p;
}

Peer::Peer(const CoinCfg& cfg, const std::string& ip, int port, double timeout, bool relay) : cfg_(cfg), ip_(ip), timeout_(timeout), sock_(ip, port, timeout) {
    send("version", version_payload(cfg.protocol, relay));
    bool got_v = false, got_ack = false;
    double end = now_s() + timeout;
    std::string cmd;
    Bytes pl;
    while (!(got_v && got_ack)) {
        double left = end - now_s();
        if (left <= 0 || !recv(cmd, pl, left)) throw NetError("handshake timed out");
        if (cmd == "version") {
            if (pl.size() >= 12) { std::memcpy(&services, pl.data() + 4, 8); }
            if (pl.size() >= 85) { int32_t h; std::memcpy(&h, pl.data() + pl.size() - 5, 4); start_height = h; }
            if (cfg.mode == Mode::p2p && !((services & 1) && (services & 8))) throw NetError("peer is not an archival witness node");
            got_v = true;
            send("verack");
        } else if (cmd == "verack") {
            got_ack = true;
        }
    }
}

void Peer::send(const std::string& cmd, const Bytes& payload) {
    Bytes m(24);
    uint32_t magic = cfg_.magic;
    std::memcpy(m.data(), &magic, 4);
    std::memset(m.data() + 4, 0, 12);
    std::memcpy(m.data() + 4, cmd.data(), std::min<size_t>(12, cmd.size()));
    uint32_t len = uint32_t(payload.size());
    std::memcpy(m.data() + 16, &len, 4);
    Hash32 cs = sha256d(payload);
    std::memcpy(m.data() + 20, cs.data(), 4);
    m.insert(m.end(), payload.begin(), payload.end());
    sock_.send_all(m.data(), m.size(), timeout_);
}

bool Peer::recv(std::string& cmd, Bytes& payload, double timeout) {
    double end = now_s() + std::max(0.1, timeout);
    uint8_t tmp[1 << 16];
    while (true) {
        if (frame_next(buf_, cfg_.magic, cmd, payload)) {
            if (cmd == "ping") { send("pong", payload); continue; }
            return true;
        }
        double left = end - now_s();
        if (left <= 0) return false;
        long r = sock_.recv_some(tmp, sizeof tmp, std::min(left, 15.0));
        if (r == 0) throw NetError("peer closed the connection");
        if (r > 0) buf_.insert(buf_.end(), tmp, tmp + r);
    }
}

std::map<std::string, Bytes> Peer::get_blocks(const std::vector<Hash32>& hashes) {
    Bytes pl;
    put_compact(pl, hashes.size());
    for (auto& h : hashes) {
        uint32_t t = MSG_WITNESS_BLOCK;
        pl.insert(pl.end(), reinterpret_cast<uint8_t*>(&t), reinterpret_cast<uint8_t*>(&t) + 4);
        pl.insert(pl.end(), h.begin(), h.end());
    }
    send("getdata", pl);
    std::map<std::string, Bytes> want, out;
    for (auto& h : hashes) want[hash_key(h)];
    size_t hl = header_len(cfg_.family);
    double end = now_s() + timeout_ * 3;
    std::string cmd;
    Bytes p;
    while (!want.empty() && now_s() < end) {
        if (!recv(cmd, p, end - now_s())) break;
        if (cmd == "block" && p.size() >= hl) {
            std::string k = hash_key(sha256d(View(p.data(), hl)));
            if (want.erase(k)) out[k] = std::move(p);
        } else if (cmd == "notfound") {
            break;
        }
    }
    return out;
}

std::vector<Bytes> Peer::getheaders(const std::vector<Hash32>& locator, size_t hl) {
    Bytes pl;
    uint32_t pv = uint32_t(cfg_.protocol);
    pl.insert(pl.end(), reinterpret_cast<uint8_t*>(&pv), reinterpret_cast<uint8_t*>(&pv) + 4);
    put_compact(pl, locator.size());
    for (auto& h : locator) pl.insert(pl.end(), h.begin(), h.end());
    pl.insert(pl.end(), 32, 0);
    send("getheaders", pl);
    double end = now_s() + timeout_;
    std::string cmd;
    Bytes p;
    while (now_s() < end) {
        if (!recv(cmd, p, end - now_s())) break;
        if (cmd != "headers") continue;
        Reader r(p);
        uint64_t n = r.varint();
        std::vector<Bytes> out;
        for (uint64_t k = 0; k < n; k++) { auto h = r.read(hl); out.emplace_back(h.begin(), h.end()); r.varint(); }
        return out;
    }
    throw NetError("no headers reply");
}

}  // namespace bsx
