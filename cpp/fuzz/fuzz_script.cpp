// libFuzzer target: the fuzz input is a revealed script (as the last witness item of a non-coinbase input) and, split at its first byte,
// a stack item; drives parse_pushes, every template (secret-hash HTLC, 2-of-2 lock, refund, BCH covenant) and the tier logic.
#include "../src/scan.hpp"
using namespace bsx;
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    if (size < 1) return 0;
    size_t cut = std::min<size_t>(data[0], size - 1);
    Tx t;
    t.version = 2;
    TxIn in;
    in.prev[0] = 1;
    in.sequence = size > 2 ? data[1] : 0;
    in.witness.push_back(Bytes(data + 1, data + 1 + cut));
    in.witness.push_back(Bytes(data + 1 + cut, data + size));
    in.script_sig = Bytes(data + 1, data + 1 + cut);
    t.vin.push_back(in);
    TxOut o;
    o.value = 1000;
    o.script = Bytes{0x00, 0x14, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20};
    t.vout.push_back(o);
    std::vector<Tx> txs{t};
    for (Family f : {Family::btc, Family::bch, Family::part}) scan_block(f, 1, 0, txs, nullptr, true, 300, false);
    parse_pushes(View(data, size));
    return 0;
}
