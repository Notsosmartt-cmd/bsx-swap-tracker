#include "source.hpp"
#include <algorithm>
#include <array>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <map>
#include <mutex>
#include <openssl/evp.h>
#include <random>
#include <set>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include "settings.hpp"
#include "util.hpp"

namespace bsx {

static Hash32 from_display(const std::string& hexs) {
    Hash32 h{};
    for (size_t k = 0; k < 32; k++) h[31 - k] = uint8_t(std::stoi(hexs.substr(2 * k, 2), nullptr, 16));
    return h;
}

// ------------------------------------------------------------------ proof of work

bool pow_ok(View hdr, bool scrypt) {
    if (hdr.size() < 80) return false;
    Hash32 h;
    if (scrypt) {
        if (EVP_PBE_scrypt(reinterpret_cast<const char*>(hdr.data()), 80, hdr.data(), 80, 1024, 1, 1, 0, h.data(), 32) != 1) return false;
    } else {
        h = sha256d(hdr.subspan(0, 80));
    }
    uint32_t bits;
    std::memcpy(&bits, hdr.data() + 72, 4);
    // target = mantissa * 256^(exponent - 3), as a 32-byte big-endian number; the hash is a little-endian number
    uint8_t target[32] = {0};
    int exp = int(bits >> 24);
    uint32_t mant = bits & 0xFFFFFF;
    for (int k = 0; k < 3; k++) {
        int pos = 31 - (exp - 3) - k;                    // byte index (big-endian) of mantissa byte k (k=0 least significant)
        if (pos >= 0 && pos < 32) target[pos] = uint8_t(mant >> (8 * k));
        else if (pos < 0 && (mant >> (8 * k)) & 0xFF) return false;   // target overflows 256 bits: never a valid header here
    }
    for (int k = 0; k < 32; k++) {
        uint8_t hb = h[31 - k];
        if (hb != target[k]) return hb < target[k];
    }
    return true;
}

// 32-byte big-endian target of a compact "bits" value (all zero for an invalid encoding)
static std::array<uint8_t, 32> target_of(uint32_t bits) {
    std::array<uint8_t, 32> t{};
    int exp = int(bits >> 24);
    uint32_t mant = bits & 0xFFFFFF;
    for (int k = 0; k < 3; k++) {
        int pos = 31 - (exp - 3) - k;
        if (pos >= 0 && pos < 32) t[pos] = uint8_t(mant >> (8 * k));
    }
    return t;
}

static std::array<uint8_t, 32> times4(std::array<uint8_t, 32> t) {   // t << 2, saturating
    int carry = 0;
    for (int i = 31; i >= 0; i--) { int v = (t[i] << 2) | carry; t[i] = uint8_t(v); carry = v >> 8; }
    if (carry) t.fill(0xFF);
    return t;
}

// ------------------------------------------------------------------ Electrum header file (read-only) + extension cache

class HeaderChain {
public:
    explicit HeaderChain(const std::string& path) : path_(path) { refresh(); }
    ~HeaderChain() { unmap(); }
    void refresh() {
        unmap();
        int fd = open(path_.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) throw SourceUnavailable("header file not readable: " + path_);
        struct stat st;
        fstat(fd, &st);
        size_ = size_t(st.st_size);
        if (size_ < 160) { ::close(fd); throw SourceUnavailable("header file too small"); }
        map_ = static_cast<uint8_t*>(mmap(nullptr, size_, PROT_READ, MAP_SHARED, fd, 0));
        ::close(fd);
        if (map_ == MAP_FAILED) { map_ = nullptr; throw SourceUnavailable("mmap failed"); }
        n_ = int64_t(size_ / 80);
        static const uint8_t zero[80] = {0};
        int64_t i = n_ - 1;
        while (i > 0) {                                  // walk back while the run is real and linked
            const uint8_t* cur = map_ + i * 80;
            const uint8_t* prev = map_ + (i - 1) * 80;
            if (!std::memcmp(cur, zero, 80) || !std::memcmp(prev, zero, 80)) break;
            Hash32 ph = sha256d(View(prev, 80));
            if (std::memcmp(cur + 4, ph.data(), 32)) break;
            i--;
        }
        first = i;
        tip = n_ - 1;
    }
    View header(int64_t h) const {
        if (h < first || h > tip) throw SourceUnavailable(fmt("height %lld outside the verified header window %lld-%lld", (long long)h, (long long)first, (long long)tip));
        return View(map_ + h * 80, 80);
    }
    int64_t first = 0, tip = 0;

private:
    void unmap() { if (map_) { munmap(map_, size_); map_ = nullptr; } }
    std::string path_;
    uint8_t* map_ = nullptr;
    size_t size_ = 0;
    int64_t n_ = 0;
};

class HeaderView {
public:
    virtual ~HeaderView() = default;
    virtual void refresh() = 0;
    virtual View header(int64_t h) const = 0;
    Hash32 hash(int64_t h) const { return sha256d(header(h)); }
    uint32_t time(int64_t h) const { uint32_t t; std::memcpy(&t, header(h).data() + 68, 4); return t; }
    int64_t first = 0, tip = 0;
};

class ExtendedHeaders : public HeaderView {
public:
    ExtendedHeaders(const std::string& live, const std::string& cache) : live_(live), cache_path_(cache) { refresh(); }
    void refresh() override {
        live_.refresh();
        cache_start_ = -1;
        cache_.clear();
        try {
            std::string d = read_text(cache_path_);
            if (d.size() >= 84) {
                uint32_t start;
                std::memcpy(&start, d.data(), 4);
                size_t n = (d.size() - 4) / 80;
                Hash32 lh = sha256d(View(reinterpret_cast<const uint8_t*>(d.data()) + 4 + (n - 1) * 80, 80));
                View lf = live_.header(live_.first);
                if (n && int64_t(start + n) == live_.first && !std::memcmp(lf.data() + 4, lh.data(), 32)) {
                    cache_start_ = start;
                    cache_.assign(d.begin() + 4, d.begin() + 4 + n * 80);
                }
            }
        } catch (...) {
        }
        first = cache_start_ >= 0 ? cache_start_ : live_.first;
        tip = live_.tip;
    }
    View header(int64_t h) const override {
        if (h >= live_.first) return live_.header(h);
        if (cache_start_ >= 0 && h >= cache_start_ && h < live_.first) return View(cache_.data() + (h - cache_start_) * 80, 80);
        throw SourceUnavailable(fmt("height %lld outside the verified header window", (long long)h));
    }
private:
    HeaderChain live_;
    std::string cache_path_;
    int64_t cache_start_ = -1;
    Bytes cache_;
};

// ------------------------------------------------------------------ read-ahead from several peers (BlockPrefetcher)

class Prefetcher {
public:
    using Connect = std::function<std::unique_ptr<Peer>()>;
    Prefetcher(Connect c, std::function<Hash32(int64_t)> hash_of, std::function<int64_t()> tip_of, int workers, int batch, size_t max_bytes, std::string name)
        : connect_(std::move(c)), hash_of_(std::move(hash_of)), tip_of_(std::move(tip_of)), batch_(batch), max_bytes_(max_bytes), name_(std::move(name)) {
        for (int i = 0; i < workers; i++) threads_.emplace_back([this, i] { worker(i); });
    }
    ~Prefetcher() { close(); }
    void close() {
        { std::lock_guard<std::mutex> g(mx_); stop_ = true; }
        cv_.notify_all();
        for (auto& t : threads_) if (t.joinable()) t.join();
        threads_.clear();
    }
    Bytes get(int64_t h, double timeout = 90) {
        std::unique_lock<std::mutex> g(mx_);
        if (base_ < 0 || h < base_ || h > base_ + window_) { for (auto& kv : results_) nbytes_ -= kv.second.size(); results_.clear(); }
        for (auto it = results_.begin(); it != results_.end();) {
            if (it->first < h) { nbytes_ -= it->second.size(); it = results_.erase(it); } else ++it;
        }
        base_ = h;
        cv_.notify_all();
        double end = now_s() + timeout;
        while (!results_.count(h)) {
            if (stop_ || now_s() > end) throw SourceUnavailable(fmt("no peer delivered block %lld within %.0f s", (long long)h, timeout));
            cv_.wait_for(g, std::chrono::milliseconds(500));
        }
        Bytes raw = std::move(results_[h]);
        results_.erase(h);
        nbytes_ -= raw.size();
        cv_.notify_all();
        return raw;
    }

private:
    std::vector<int64_t> next_batch() {
        std::vector<int64_t> out;
        if (base_ < 0) return out;
        int64_t tip = tip_of_();
        for (int64_t h = base_; h <= tip && h < base_ + window_ && int(out.size()) < batch_; h++) {
            if (!results_.count(h) && !inflight_.count(h)) {
                if (nbytes_ + inflight_.size() * size_t(avg_) >= max_bytes_ && h != base_) break;
                out.push_back(h);
            } else if (!out.empty()) {
                break;
            }
        }
        return out;
    }
    void worker(int idx) {
        char nm[16];
        snprintf(nm, sizeof nm, "pf-%s-%d", name_.c_str(), idx);
        pthread_setname_np(pthread_self(), nm);
        std::unique_ptr<Peer> peer;
        int errs = 0;
        while (true) {
            std::vector<int64_t> batch;
            {
                std::unique_lock<std::mutex> g(mx_);
                while (!stop_) {
                    batch = next_batch();
                    if (!batch.empty()) break;
                    cv_.wait_for(g, std::chrono::milliseconds(500));
                }
                if (stop_) return;
                for (auto h : batch) inflight_.insert(h);
            }
            std::map<std::string, Bytes> got;
            std::vector<Hash32> hashes;
            try {
                if (!peer) peer = connect_();
                for (auto h : batch) hashes.push_back(hash_of_(h));
                got = peer->get_blocks(hashes);
                errs = 0;
            } catch (...) {
                errs++;
                peer.reset();
            }
            {
                std::lock_guard<std::mutex> g(mx_);
                if (got.empty() && peer) peer.reset();          // a peer that answers none of a batch does not have these blocks
                for (size_t k = 0; k < batch.size(); k++) {
                    inflight_.erase(batch[k]);
                    if (k < hashes.size()) {
                        auto it = got.find(hash_key(hashes[k]));
                        if (it != got.end() && base_ >= 0 && batch[k] >= base_ && !results_.count(batch[k])) {
                            nbytes_ += it->second.size();
                            delivered_++;
                            avg_ += (double(it->second.size()) - avg_) / double(std::min<int64_t>(delivered_, 50));
                            results_[batch[k]] = std::move(it->second);
                        }
                    }
                }
                cv_.notify_all();
            }
            if (!peer) std::this_thread::sleep_for(std::chrono::milliseconds(errs ? int(1000 * std::min(30.0, 1.0 * errs)) : 200));
        }
    }
    Connect connect_;
    std::function<Hash32(int64_t)> hash_of_;
    std::function<int64_t()> tip_of_;
    int batch_;
    size_t max_bytes_;
    std::string name_;
    int64_t window_ = 4096, base_ = -1, delivered_ = 0;
    double avg_ = 0;
    size_t nbytes_ = 0;
    bool stop_ = false;
    std::map<int64_t, Bytes> results_;
    std::set<int64_t> inflight_;
    std::mutex mx_;
    std::condition_variable cv_;
    std::vector<std::thread> threads_;
};

// ------------------------------------------------------------------ built-in header sync (no Electrum files needed)

// Headers of BTC / LTC from a trusted checkpoint forward, fetched from network peers with getheaders; every header must link to the previous
// one, satisfy its own proof of work, and have a target at most 4x the checkpoint's (a peer cannot feed a cheap low-difficulty fake chain).
// Stored in <data_dir>/headers_<COIN>_sync.bin (80 bytes each, from checkpoint + 1) with a .json holding the first height. Reorgs: a
// longer branch replaces ours from the fork point; a peer whose chain ends below ours (stalled, or a shorter branch) is ignored.
class SyncedHeaders : public HeaderView {
public:
    using Fetch = std::function<std::vector<Bytes>(const std::vector<Hash32>&)>;   // locator -> up to 2000 headers after it
    SyncedHeaders(const CoinCfg& cfg, std::function<std::unique_ptr<Peer>()> connect)
        : SyncedHeaders(cfg, Fetch(), settings().data_dir + "/headers_" + cfg.coin + "_sync.bin") {
        connect_ = std::move(connect);
        fetch_ = [this](const std::vector<Hash32>& loc) {
            if (!peer_) peer_ = connect_();
            return peer_->getheaders(loc, 80);
        };
        refresh();
    }
    SyncedHeaders(const CoinCfg& cfg, Fetch fetch, std::string path)     // also used by the self-test with a simulated peer
        : cfg_(cfg), fetch_(std::move(fetch)), path_(std::move(path)), floor_(times4(target_of(cfg.checkpoint_bits))) {
        for (size_t k = 0; k < 32; k++) anchor_[31 - k] = uint8_t(std::stoi(cfg.checkpoint_hash.substr(2 * k, 2), nullptr, 16));
        try {
            std::string d = read_text(path_);
            for (size_t i = 0; i + 80 <= d.size(); i += 80) h_.emplace_back(d.begin() + i, d.begin() + i + 80);
        } catch (...) {
        }
        set_range();
    }
    void sync_once() { sync(); }
    void refresh() override {
        if (now_s() - last_sync_ < 20) return;       // tip() is called often; peers announce a block every few minutes at most
        last_sync_ = now_s();
        for (int attempt = 0; attempt < 6; attempt++) {         // each attempt a fresh peer; headers already fetched are kept
            try { sync(); return; }
            catch (const NetError& e) {
                peer_.reset();
                LOG_WARN(fmt("%s: header sync peer lost at height %lld (%s), %s", cfg_.coin.c_str(), (long long)(cfg_.checkpoint_height + int64_t(h_.size())), e.what(), attempt == 5 ? "giving up" : "trying another"));
                if (attempt == 5) throw SourceUnavailable(cfg_.coin + " header sync: " + e.what());
            }
        }
    }
    View header(int64_t h) const override {
        if (h < first || h > tip) throw SourceUnavailable(fmt("height %lld outside the synced header window %lld-%lld", (long long)h, (long long)first, (long long)tip));
        return View(h_[size_t(h - first)].data(), 80);
    }

private:
    void set_range() { first = cfg_.checkpoint_height + 1; tip = cfg_.checkpoint_height + int64_t(h_.size()); }
    bool target_ok(const Bytes& hd) const {
        uint32_t bits;
        std::memcpy(&bits, hd.data() + 72, 4);
        auto t = target_of(bits);
        return std::lexicographical_compare(t.begin(), t.end(), floor_.begin(), floor_.end()) || t == floor_;
    }
    void sync() {
        size_t added = 0, unsaved = 0;
        backup_.clear();
        try {
            sync_loop(added, unsaved);
        } catch (const NetError&) {
            keep_longer();
            if (unsaved) save();                         // a later attempt or run continues from here
            throw;
        }
        if (keep_longer()) unsaved = 1;
        if (unsaved) save();
        if (added >= 2000) LOG_INFO(fmt("%s: synced headers to height %lld", cfg_.coin.c_str(), (long long)tip));
    }
    void sync_loop(size_t& added, size_t& unsaved) {
        int batches = 0;
        while (true) {
            std::vector<Hash32> loc;
            for (int64_t i = int64_t(h_.size()) - 1, step = 1; i >= 0 && loc.size() < 30; i -= step) { loc.push_back(sha256d(h_[size_t(i)])); if (loc.size() > 8) step *= 2; }
            loc.push_back(anchor_);
            auto got = fetch_(loc);
            if (got.empty()) break;
            Hash32 prev;
            std::memcpy(prev.data(), got[0].data() + 4, 32);
            size_t keep = h_.size();
            if (prev == anchor_) keep = 0;
            else if (h_.empty() || prev != sha256d(h_.back())) {
                int64_t at = -1;
                for (int64_t i = int64_t(h_.size()) - 1; i >= 0; i--) if (sha256d(h_[size_t(i)]) == prev) { at = i; break; }
                if (at < 0) throw NetError("headers do not attach to the checkpoint chain");
                keep = size_t(at + 1);
            }
            if (keep < h_.size() && backup_.empty()) backup_ = h_;   // a peer behind us or on another branch: decided at the end
            Hash32 link = keep ? sha256d(h_[keep - 1]) : anchor_;
            for (auto& hd : got) {
                if (std::memcmp(hd.data() + 4, link.data(), 32)) throw NetError("headers not linked");
                if (!pow_ok(hd, cfg_.scrypt)) throw NetError("header fails its proof of work");
                if (!target_ok(hd)) throw NetError("header difficulty below the floor");
                link = sha256d(hd);
            }
            h_.resize(keep);
            for (auto& hd : got) h_.push_back(std::move(hd));
            added += got.size();
            unsaved += got.size();
            set_range();
            if (++batches % 20 == 0 && (backup_.empty() || h_.size() >= backup_.size())) { save(); unsaved = 0; }
            if (got.size() < 2000) break;
        }
    }
    // A peer whose chain ends below ours (stalled, or on a shorter branch) must not cut our chain back: keep the longer one.
    // Headers carry proof of work and similar difficulty near the tip, so the longer valid chain is the one with more work.
    bool keep_longer() {
        if (backup_.empty()) return false;
        bool restored = false;
        if (h_.size() < backup_.size()) {
            LOG_WARN(fmt("%s: peer's header chain ends at %lld, below ours (%lld); kept ours", cfg_.coin.c_str(),
                         (long long)(cfg_.checkpoint_height + int64_t(h_.size())), (long long)(cfg_.checkpoint_height + int64_t(backup_.size()))));
            h_ = std::move(backup_);
            peer_.reset();                               // ask another peer next time
            restored = true;
        } else {
            size_t same = 0;
            while (same < backup_.size() && h_[same] == backup_[same]) same++;
            if (same < backup_.size()) LOG_WARN(fmt("%s: reorganisation, %zu synced headers replaced", cfg_.coin.c_str(), backup_.size() - same));
        }
        backup_.clear();
        set_range();
        return restored;
    }
    void save() {
        set_range();
        {
            std::string d;
            d.reserve(h_.size() * 80);
            for (auto& x : h_) d.append(reinterpret_cast<const char*>(x.data()), 80);
            write_atomic(path_, d);
            Json m = Json::object();
            m.set("checkpoint_height", Json::integer(cfg_.checkpoint_height));
            m.set("first", Json::integer(first));
            m.set("tip", Json::integer(tip));
            m.set("updated", Json::integer(now_i()));
            write_atomic(path_.substr(0, path_.size() - 4) + ".json", json_dump(m));
        }
    }
    const CoinCfg& cfg_;
    std::function<std::unique_ptr<Peer>()> connect_;
    Fetch fetch_;
    std::string path_;
    std::array<uint8_t, 32> floor_;
    Hash32 anchor_{};
    std::vector<Bytes> h_, backup_;
    std::unique_ptr<Peer> peer_;
    double last_sync_ = 0;
};

// Self-test of the header sync against simulated peers serving real mainnet headers (fixture: the first headers after the BTC checkpoint).
// Returns "" when every case passes, else what failed.
std::string header_sync_selftest(const std::string& fixture) {
    std::string d;
    try { d = read_text(fixture); } catch (...) { return "fixture missing: " + fixture; }
    std::vector<Bytes> all;
    for (size_t i = 0; i + 80 <= d.size(); i += 80) all.emplace_back(d.begin() + i, d.begin() + i + 80);
    if (all.size() < 300) return "fixture too short";
    CoinCfg cfg = coin_cfg("BTC");
    Hash32 anchor{};
    for (size_t k = 0; k < 32; k++) anchor[31 - k] = uint8_t(std::stoi(cfg.checkpoint_hash.substr(2 * k, 2), nullptr, 16));
    const std::vector<Bytes>* peer_chain = nullptr;
    auto fetch = [&](const std::vector<Hash32>& loc) {               // what a node does with getheaders: first locator hash it knows
        std::vector<Bytes> out;
        size_t start = SIZE_MAX;
        for (auto& h : loc) {
            if (h == anchor) { start = 0; break; }
            for (size_t i = 0; i < peer_chain->size(); i++) if (sha256d((*peer_chain)[i]) == h) { start = i + 1; break; }
            if (start != SIZE_MAX) break;
        }
        if (start == SIZE_MAX) return out;
        for (size_t i = start; i < peer_chain->size() && out.size() < 2000; i++) out.push_back((*peer_chain)[i]);
        return out;
    };
    const char* tmp = getenv("TMPDIR");
    std::string path = std::string(tmp && *tmp ? tmp : "/tmp") + "/bsx_header_sync_selftest_" + std::to_string(getpid()) + ".bin";
    auto cleanup = [&] { unlink(path.c_str()); unlink((path.substr(0, path.size() - 4) + ".json").c_str()); };
    cleanup();
    std::vector<Bytes> full(all.begin(), all.begin() + 300), stale(all.begin(), all.begin() + 200);
    std::string err;
    {
        SyncedHeaders s(cfg, fetch, path);
        peer_chain = &stale;
        s.sync_once();
        if (s.tip != cfg.checkpoint_height + 200) err = "sync from an empty start";
        peer_chain = &full;
        if (err.empty()) { s.sync_once(); if (s.tip != cfg.checkpoint_height + 300) err = "extension to a longer chain"; }
        peer_chain = &stale;                                         // a stalled peer that knows only the first 200
        if (err.empty()) { s.sync_once(); if (s.tip != cfg.checkpoint_height + 300) err = "a stalled peer cut the chain back"; }
    }
    if (err.empty()) {
        SyncedHeaders again(cfg, fetch, path);                       // what was saved survives a restart
        if (again.tip != cfg.checkpoint_height + 300) err = "saved chain after the stalled peer";
    }
    cleanup();
    return err;
}

// ------------------------------------------------------------------ BTC / LTC from network peers

class P2PSource : public Source {
public:
    // header source: BasicSwap's Electrum header file (plus the older-header cache written by the Python header walk) when present, else the
    // built-in header sync from the coin's checkpoint
    P2PSource(const CoinCfg& cfg, const std::string& out_dir) : cfg_(cfg) {
        (void)out_dir;
        std::string live = settings().electrum_headers_dir + "/" + cfg.headers;
        if (!settings().electrum_headers_dir.empty() && access(live.c_str(), R_OK) == 0) {
            chain_ = std::make_unique<ExtendedHeaders>(live, settings().data_dir + "/headers_" + cfg.coin + ".bin");
            header_source = "electrum";
        } else {
            chain_ = std::make_unique<SyncedHeaders>(cfg, [this] { return new_peer(); });
            header_source = "sync";
        }
    }
    std::string header_source;
    ~P2PSource() override { close(); }
    int64_t tip() override { chain_->refresh(); return chain_->tip; }
    int64_t tip_time() override { return chain_->time(chain_->tip); }
    int64_t earliest(int64_t want, int64_t) override { return std::max(want, chain_->first + 1); }
    std::string blockhash(int64_t h) override { return hex_rev(chain_->hash(h)); }
    Block block(int64_t h, const std::string&) override {
        View hdr = chain_->header(h);
        Hash32 want = sha256d(hdr);
        if (!pow_ok(hdr, cfg_.scrypt)) throw SourceUnavailable(fmt("header at %lld fails its own proof of work", (long long)h));
        double t0 = now_s();
        if (!pf_) pf_ = std::make_unique<Prefetcher>([this] { return new_peer(); }, [this](int64_t x) { return chain_->hash(x); }, [this] { return chain_->tip; },
                                                    cfg_.workers, cfg_.batch, cfg_.max_bytes, cfg_.coin);
        Bytes raw = pf_->get(h);
        if (sha256d(View(raw.data(), 80)) != want) throw SourceUnavailable(fmt("peer sent a different block than the header at %lld", (long long)h));
        Block b = parse_block(raw, cfg_.family);
        Hash32 mr = merkle_root(b.txs);
        if (std::memcmp(mr.data(), hdr.data() + 36, 32)) throw SourceUnavailable(fmt("merkle root mismatch at height %lld", (long long)h));
        lat.push_back(now_s() - t0);
        if (lat.size() > 50) lat.erase(lat.begin(), lat.end() - 50);
        return b;
    }
    void close() override { pf_.reset(); }

private:
    std::unique_ptr<Peer> new_peer() {
        std::vector<std::string> names = cfg_.seeds;
        static std::mt19937 rng(std::random_device{}());
        std::shuffle(names.begin(), names.end(), rng);
        int tried = 0;
        for (auto& n : names) {
            auto addrs = resolve_ipv4(n, cfg_.p2p_port);
            std::shuffle(addrs.begin(), addrs.end(), rng);
            if (addrs.size() > 12) addrs.resize(12);
            for (auto& ip : addrs) {
                {
                    std::lock_guard<std::mutex> g(mx_);
                    if (bad_.count(ip) || in_use_.count(ip)) continue;
                    in_use_.insert(ip);
                }
                tried++;
                try {
                    auto p = std::make_unique<Peer>(cfg_, ip, cfg_.p2p_port, 15);
                    LOG_INFO(cfg_.coin + ": connected to archival peer " + ip);
                    return p;
                } catch (...) {
                    std::lock_guard<std::mutex> g(mx_);
                    bad_.insert(ip);
                    in_use_.erase(ip);
                    if (tried >= 40) break;
                }
            }
        }
        throw NetError("no archival peer found");
    }
    const CoinCfg& cfg_;
    std::unique_ptr<HeaderView> chain_;
    std::unique_ptr<Prefetcher> pf_;
    std::mutex mx_;
    std::set<std::string> bad_, in_use_;
};

// ------------------------------------------------------------------ PART from the local particld P2P port

class PartHeaders {
public:
    PartHeaders(const std::string& path, int keep) : path_(path), meta_(path.substr(0, path.size() - 4) + ".json"), keep_(keep) {
        try {
            Json m = json_parse(read_text(meta_));
            first = m.get("first") ? m.get("first")->as_int() : 0;
            std::string d = read_text(path_);
            for (size_t i = 0; i + 112 <= d.size(); i += 112) h_.emplace_back(d.begin() + i, d.begin() + i + 112);
        } catch (...) {
            first = 0;
            h_.clear();
        }
    }
    int64_t tip() const { return first + int64_t(h_.size()) - 1; }
    Hash32 hash(int64_t h) const { return sha256d(at(h)); }
    uint32_t time(int64_t h) const { uint32_t t; std::memcpy(&t, at(h).data() + 100, 4); return t; }
    const Bytes& at(int64_t h) const {
        if (h < first || h > tip()) throw SourceUnavailable(fmt("PART height %lld outside the stored headers", (long long)h));
        return h_[size_t(h - first)];
    }
    void sync(Peer& peer, const Hash32& genesis, double max_seconds = 600) {
        double t0 = now_s();
        size_t added = 0;
        while (now_s() - t0 < max_seconds) {
            std::vector<Hash32> loc;
            if (h_.empty()) loc.push_back(genesis);
            else {
                int64_t i = int64_t(h_.size()) - 1, step = 1;
                while (i >= 0 && loc.size() < 30) { loc.push_back(sha256d(h_[size_t(i)])); i -= step; if (loc.size() > 8) step *= 2; }
                if (first == 0) loc.push_back(genesis);
            }
            auto got = peer.getheaders(loc, 112);
            if (got.empty()) break;
            Hash32 prev;
            std::memcpy(prev.data(), got[0].data() + 4, 32);
            if (!h_.empty() && prev != sha256d(h_.back())) {
                int64_t at = -1;
                for (int64_t i = int64_t(h_.size()) - 1; i >= 0; i--) if (sha256d(h_[size_t(i)]) == prev) { at = i; break; }
                if (at < 0) throw SourceUnavailable("PART headers do not attach to the stored chain");
                LOG_WARN(fmt("PART: reorganisation, %lld stored headers replaced", (long long)(h_.size() - 1 - size_t(at))));
                h_.resize(size_t(at + 1));
            } else if (h_.empty() && prev != genesis) {
                throw SourceUnavailable("first PART header does not follow the genesis block");
            }
            for (size_t k = 1; k < got.size(); k++) if (std::memcmp(got[k].data() + 4, sha256d(got[k - 1]).data(), 32)) throw SourceUnavailable("PART headers not linked");
            if (h_.empty()) first = 1;
            for (auto& g : got) h_.push_back(std::move(g));
            added += got.size();
            if (int64_t(h_.size()) > keep_ + 20000) { int64_t cut = int64_t(h_.size()) - keep_; first += cut; h_.erase(h_.begin(), h_.begin() + cut); }
            if (got.size() < 2000) break;
        }
        if (int64_t(h_.size()) > keep_) { int64_t cut = int64_t(h_.size()) - keep_; first += cut; h_.erase(h_.begin(), h_.begin() + cut); }
        if (added) save();
    }
    int64_t first = 0;

private:
    void save() {
        std::string d;
        d.reserve(h_.size() * 112);
        for (auto& x : h_) d.append(reinterpret_cast<const char*>(x.data()), 112);
        write_atomic(path_, d);
        Json m = Json::object();
        m.set("first", Json::integer(first));
        m.set("tip", Json::integer(tip()));
        m.set("updated", Json::integer(now_i()));
        write_atomic(meta_, json_dump(m));
    }
    std::string path_, meta_;
    int64_t keep_;
    std::vector<Bytes> h_;
};

class PartSource : public Source {
public:
    PartSource(const CoinCfg& cfg, const std::string& out_dir) : cfg_(cfg), chain_(out_dir + "/headers_PART.bin", 400 * cfg.bpd), genesis_(from_display(cfg.genesis)) {
        chain_.sync(peer(), genesis_);
    }
    int64_t tip() override { chain_.sync(peer(), genesis_); return chain_.tip(); }
    int64_t tip_time() override { return chain_.time(chain_.tip()); }
    int64_t earliest(int64_t want, int64_t) override { return std::max(want, chain_.first); }
    std::string blockhash(int64_t h) override { return hex_rev(chain_.hash(h)); }
    Block block(int64_t h, const std::string&) override {
        double t0 = now_s();
        Hash32 want = chain_.hash(h);
        Bytes raw;
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                auto got = peer().get_blocks({want});
                auto it = got.find(hash_key(want));
                if (it == got.end()) throw NetError("particld does not have the block");
                raw = std::move(it->second);
                break;
            } catch (const NetError&) {
                peer_.reset();
                if (attempt) throw SourceUnavailable(fmt("PART block %lld not received", (long long)h));
            }
        }
        Block b = parse_block(raw, Family::part);
        Hash32 mr = merkle_root(b.txs);
        if (std::memcmp(mr.data(), raw.data() + 36, 32)) throw SourceUnavailable("merkle root mismatch: Particl parser and block disagree");
        lat.push_back(now_s() - t0);
        if (lat.size() > 50) lat.erase(lat.begin(), lat.end() - 50);
        return b;
    }
    void close() override { peer_.reset(); }

private:
    Peer& peer() {
        if (!peer_) {
            for (int attempt = 0; !peer_; attempt++) {     // the LOCAL particld only; a burst of local clients can get a reset: retry briefly
                try { peer_ = std::make_unique<Peer>(cfg_, "127.0.0.1", cfg_.p2p_port, 30); }
                catch (const NetError& e) {
                    if (attempt >= 2) throw SourceUnavailable(std::string("PART: ") + e.what());
                    std::this_thread::sleep_for(std::chrono::seconds(2 + 3 * attempt));
                }
            }
        }
        return *peer_;
    }
    const CoinCfg& cfg_;
    PartHeaders chain_;
    Hash32 genesis_;
    std::unique_ptr<Peer> peer_;
};

// ------------------------------------------------------------------ RPC (local daemon, raw blocks)

class RpcSource : public Source {
public:
    explicit RpcSource(const CoinCfg& cfg) : cfg_(cfg), rpc_(make_rpc(cfg)) {}
    bool is_rpc() const override { return true; }
    int64_t tip() override { return wrap([&] { return rpc_.call("getblockcount").as_int(); }); }
    int64_t tip_time() override {
        return wrap([&] {
            Json p = Json::array(); p.push(rpc_.call("getbestblockhash")); p.push(Json::integer(1));
            return rpc_.call("getblock", p).get("time")->as_int();
        });
    }
    int64_t earliest(int64_t lo, int64_t hi) override {
        auto avail = [&](int64_t h) {
            try {
                Json p = Json::array(); p.push(Json::integer(h));
                Json q = Json::array(); q.push(rpc_.call("getblockhash", p)); q.push(Json::integer(1));
                rpc_.call("getblock", q);
                return true;
            } catch (const BlockPruned&) { return false; }
        };
        if (avail(lo)) return lo;
        while (hi - lo > 1) { int64_t mid = (lo + hi) / 2; if (avail(mid)) hi = mid; else lo = mid; }
        return hi;
    }
    std::string blockhash(int64_t h) override {
        return wrap([&] { Json p = Json::array(); p.push(Json::integer(h)); return rpc_.call("getblockhash", p).as_str(); });
    }
    Block block(int64_t, const std::string& bh) override {
        std::string hexs = wrap([&] { Json p = Json::array(); p.push(Json::str(bh)); p.push(Json::integer(0)); return rpc_.call("getblock", p).as_str(); });
        Bytes raw(hexs.size() / 2);
        for (size_t k = 0; k < raw.size(); k++) raw[k] = uint8_t(std::stoi(hexs.substr(2 * k, 2), nullptr, 16));
        lat = rpc_.lat;
        return parse_block(raw, cfg_.family);
    }

private:
    template <class F> auto wrap(F f) -> decltype(f()) {
        try { return f(); } catch (const RpcUnavailable& e) { throw SourceUnavailable(e.what()); }
    }
    const CoinCfg& cfg_;
    ChainRPC rpc_;
};

std::unique_ptr<Source> make_source(const CoinCfg& cfg, const std::string& out_dir) {
    try {
        switch (cfg.mode) {
            case Mode::p2p: return std::make_unique<P2PSource>(cfg, out_dir);
            case Mode::part_p2p: return std::make_unique<PartSource>(cfg, out_dir);
            case Mode::rpc_raw: return std::make_unique<RpcSource>(cfg);
        }
    } catch (const NetError& e) {
        throw SourceUnavailable(cfg.coin + ": " + e.what());
    }
    throw SourceUnavailable("unknown mode");
}

}  // namespace bsx
