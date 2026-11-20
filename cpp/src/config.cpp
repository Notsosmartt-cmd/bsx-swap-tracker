#include "config.hpp"
#include <stdexcept>

namespace bsx {

static std::vector<CoinCfg> make() {
    std::vector<CoinCfg> v;
    CoinCfg btc;
    btc.coin = "BTC"; btc.family = Family::btc; btc.mode = Mode::p2p; btc.bpd = 144; btc.rps = 10;
    btc.magic = 0xD9B4BEF9; btc.p2p_port = 8333; btc.workers = 4; btc.batch = 6; btc.max_bytes = 60u << 20;
    btc.seeds = {"dnsseed.emzy.de", "seed.btc.petertodd.net", "seed.mainnet.achownodes.xyz"};
    btc.headers = "bitcoin/mainnet/blockchain_headers";
    btc.genesis = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f";
    btc.p2sh_retain_days = 7; btc.wide_max_hex = 140;
    btc.checkpoint_height = 950400; btc.checkpoint_hash = "00000000000000000001ad32df502fcd7f683eacc94c7275ccd58282c05c0735"; btc.checkpoint_bits = 0x17020f79;   // 2026-05
    v.push_back(btc);
    CoinCfg ltc;
    ltc.coin = "LTC"; ltc.family = Family::ltc; ltc.mode = Mode::p2p; ltc.bpd = 576; ltc.rps = 40;
    ltc.magic = 0xDBB6C0FB; ltc.p2p_port = 9333; ltc.scrypt = true; ltc.workers = 4; ltc.batch = 24; ltc.max_bytes = 24u << 20;
    ltc.seeds = {"dnsseed.koin-project.com", "dnsseed.litecointools.com", "dnsseed.ltcpool.org", "dnsseed.thrasher.io", "seed-a.litecoin.loshan.co.uk", "seed-b.litecoin.loshan.co.uk"};
    ltc.headers = "litecoin/mainnet/blockchain_headers";
    ltc.genesis = "12a765e31ffd4059bada1e25190f6e98c99d9714d334efa41a195a7e7e04bfe2";
    ltc.p2sh_retain_days = 7; ltc.wide_max_hex = 140;
    ltc.checkpoint_height = 3114000; ltc.checkpoint_hash = "2345c0f3c3ad6a941674d30f4d278efab9b9c1ce6cb2ecdabf65cc453cda03dd"; ltc.checkpoint_bits = 0x192996af;
    v.push_back(ltc);
    CoinCfg bch;
    bch.coin = "BCH"; bch.family = Family::bch; bch.mode = Mode::rpc_raw; bch.bpd = 144; bch.rps = 8;
    bch.rpc_port = 19997; bch.cred_ref = "bitcoincash";
    v.push_back(bch);
    CoinCfg part;
    part.coin = "PART"; part.family = Family::part; part.mode = Mode::part_p2p; part.bpd = 720; part.rps = 60;
    part.magic = 0xB4EFF2FB; part.p2p_port = 51738; part.protocol = 90035; part.depth_days = 180;
    part.genesis = "0000ee0784c195317ac95623e22fddb8c7b8825dc3998e0bb924d66866eccf4c";
    v.push_back(part);
    return v;
}

const CoinCfg& coin_cfg(const std::string& coin) {
    static const std::vector<CoinCfg> all = make();
    for (auto& c : all) if (c.coin == coin) return c;
    throw std::invalid_argument("unknown coin " + coin + " (known: BTC, LTC, BCH, PART)");
}

std::vector<std::string> default_coins() { return {"BTC", "LTC", "BCH", "PART"}; }

}  // namespace bsx
