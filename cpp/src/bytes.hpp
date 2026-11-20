// Bounded byte reader and small helpers (port of python/collectors/chainscan_lib.py Reader).
// Every read is range-checked; nothing else in the code indexes raw buffers directly.
#pragma once
#include <cstdint>
#include <cstring>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace bsx {

using Bytes = std::vector<uint8_t>;
using View = std::span<const uint8_t>;

struct ParseError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

class Reader {
public:
    explicit Reader(View buf, size_t pos = 0) : buf_(buf), pos_(pos) {}

    View read(size_t n) {
        if (n > buf_.size() || pos_ > buf_.size() - n) throw ParseError("read past end");
        View v = buf_.subspan(pos_, n);
        pos_ += n;
        return v;
    }
    uint8_t u8() { return read(1)[0]; }
    uint16_t u16() { auto v = read(2); return uint16_t(v[0] | (v[1] << 8)); }
    uint32_t u32() { auto v = read(4); uint32_t x; std::memcpy(&x, v.data(), 4); return x; }   // little-endian host (x86-64)
    int32_t i32() { return int32_t(u32()); }
    uint64_t u64() { auto v = read(8); uint64_t x; std::memcpy(&x, v.data(), 8); return x; }
    uint64_t varint() {
        uint8_t n = u8();
        if (n < 0xFD) return n;
        if (n == 0xFD) return u16();
        if (n == 0xFE) return u32();
        return u64();
    }
    View varbytes() {
        uint64_t n = varint();
        if (n > buf_.size()) throw ParseError("read past end");
        return read(size_t(n));
    }
    uint8_t peek(size_t off = 0) const {
        if (pos_ + off >= buf_.size()) throw ParseError("read past end");
        return buf_[pos_ + off];
    }
    bool has(size_t n) const { return pos_ + n <= buf_.size(); }
    size_t pos() const { return pos_; }
    void seek(size_t p) { if (p > buf_.size()) throw ParseError("seek past end"); pos_ = p; }
    View slice(size_t a, size_t b) const {
        if (a > b || b > buf_.size()) throw ParseError("bad slice");
        return buf_.subspan(a, b - a);
    }
    size_t size() const { return buf_.size(); }

private:
    View buf_;
    size_t pos_;
};

inline std::string hex(View v) {
    static const char* d = "0123456789abcdef";
    std::string s;
    s.reserve(v.size() * 2);
    for (uint8_t b : v) { s.push_back(d[b >> 4]); s.push_back(d[b & 15]); }
    return s;
}

inline std::string hex_rev(View v) {   // byte-reversed hex (txids and block hashes are shown reversed)
    Bytes r(v.rbegin(), v.rend());
    return hex(r);
}

inline void put_compact(Bytes& out, uint64_t n) {
    if (n < 0xFD) { out.push_back(uint8_t(n)); return; }
    if (n <= 0xFFFF) { out.push_back(0xFD); out.push_back(uint8_t(n)); out.push_back(uint8_t(n >> 8)); return; }
    if (n <= 0xFFFFFFFFu) { out.push_back(0xFE); for (int i = 0; i < 4; i++) out.push_back(uint8_t(n >> (8 * i))); return; }
    out.push_back(0xFF);
    for (int i = 0; i < 8; i++) out.push_back(uint8_t(n >> (8 * i)));
}

inline void append(Bytes& out, View v) { out.insert(out.end(), v.begin(), v.end()); }

}  // namespace bsx
