// SHA-256 and double SHA-256 (FIPS 180-4), in-tree so the binary needs no crypto library. Tested against NIST vectors in `selftest`.
#pragma once
#include <array>
#include <cstdint>
#include "bytes.hpp"

namespace bsx {

using Hash32 = std::array<uint8_t, 32>;

class Sha256 {
public:
    Sha256() { reset(); }
    void reset();
    void update(const uint8_t* data, size_t len);
    void update(View v) { update(v.data(), v.size()); }
    Hash32 digest();

private:
    void block(const uint8_t* p);
    uint32_t h_[8];
    uint8_t buf_[64];
    size_t buflen_ = 0;
    uint64_t total_ = 0;
};

Hash32 sha256(View v);
Hash32 sha256d(View v);

}  // namespace bsx
