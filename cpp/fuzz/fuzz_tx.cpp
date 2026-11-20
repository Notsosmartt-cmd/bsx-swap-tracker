// libFuzzer target: one transaction (first byte picks the family), then scan_block on it. Property: parse or ParseError; for a parsed
// transaction every input/output vector is consistent and txinfo serialises to JSON that parses back.
#include <cstdlib>
#include "../src/json.hpp"
#include "../src/scan.hpp"
#include "../src/tx.hpp"
using namespace bsx;
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    if (size < 2) return 0;
    static const Family fam[4] = {Family::btc, Family::ltc, Family::bch, Family::part};
    Family f = fam[data[0] & 3];
    try {
        Reader r(View(data + 1, size - 1));
        Tx t = parse_tx(r, f);
        if (r.pos() > size - 1) abort();
        std::string info = txinfo_json(t);
        json_parse(info);
        std::vector<Tx> txs{t};
        scan_block(f, 1, 0, txs, nullptr, true, 300, false);
    } catch (const ParseError&) {
    }
    return 0;
}
