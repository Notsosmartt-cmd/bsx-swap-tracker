// libFuzzer target: P2P message framing (frame_next) on a stream of bytes for each network magic, then inv / headers payload parsing as the
// watcher does it. Property: complete messages are consumed, partial ones stay buffered, garbage throws NetError or ParseError; no crash.
#include "../src/config.hpp"
#include "../src/net.hpp"
using namespace bsx;
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    for (const char* c : {"BTC", "LTC", "PART"}) {
        const CoinCfg& cfg = coin_cfg(c);
        Bytes buf(data, data + size);
        std::string cmd;
        Bytes p;
        try {
            for (int k = 0; k < 64 && frame_next(buf, cfg.magic, cmd, p); k++) {
                Reader r(p);
                if (cmd == "inv") { uint64_t n = r.varint(); for (uint64_t i = 0; i < n && i < 50000; i++) { r.u32(); r.read(32); } }
                if (cmd == "headers") { uint64_t n = r.varint(); for (uint64_t i = 0; i < n && i < 2000; i++) { r.read(header_len(cfg.family)); r.varint(); } }
            }
        } catch (const NetError&) {
        } catch (const ParseError&) {
        }
    }
    return 0;
}
