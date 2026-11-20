// libFuzzer target: raw blocks of every family (first byte picks btc / ltc / bch / part), then merkle root and scan_block (no lookup).
// Seeds: the golden corpus blocks (python/collectors/chain/cpp_fuzz_seeds.py). Property: any input either parses or throws ParseError; no crash.
#include "../src/scan.hpp"
#include "../src/tx.hpp"
using namespace bsx;
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    if (size < 2) return 0;
    static const Family fam[4] = {Family::btc, Family::ltc, Family::bch, Family::part};
    Family f = fam[data[0] & 3];
    try {
        Block b = parse_block(View(data + 1, size - 1), f);
        merkle_root(b.txs);
        scan_block(f, 1, b.time, b.txs, nullptr, true, 300, false);
    } catch (const ParseError&) {
    }
    return 0;
}
