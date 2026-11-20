// bsx-swap-tracker: C++ runtime of the on-chain swap tracker (docs/architecture.md).
// Command dispatch, help text and the self-test; `dump` prints the canonical text of python/collectors/chain/cpp_corpus.py so the golden corpus
// can hold it to byte-identical output.
//   bsx-swap-tracker selftest
//   bsx-swap-tracker dump <btc|ltc|bch|part> <raw block file>
//   bsx-swap-tracker bench <btc|ltc|bch|part> <raw block file> [repeats]
#include <chrono>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>
#include "bytes.hpp"
#include "hash.hpp"
#include "json.hpp"
#include "script.hpp"
#include "tx.hpp"
#include "scan.hpp"
#include "settings.hpp"
#include "source.hpp"

using namespace bsx;
namespace bsx { int scan_main(int argc, char** argv); int watch_main(int argc, char** argv); int match_main(int argc, char** argv); int snapshot_main(int argc, char** argv); int report_main(int argc, char** argv); }

// Help text: `bsx-swap-tracker --help` and `bsx-swap-tracker <command> --help` (printed to stdout, exit 0).
static const char* HELP_MAIN =
"usage: bsx-swap-tracker [--config FILE] <command> [flags]\n"
"\n"
"Rebuilds BasicSwap trades from public chain data and ties them to the quotes they filled.\n"
"\n"
"commands:\n"
"  scan       read and verify blocks, record completed swaps and refunds in the database\n"
"  watch      live watcher: mempool and new blocks, early lock flags, scan after each block\n"
"  snapshot   record the order book from your own BasicSwap node\n"
"  match      tie completed swaps, refunds and early flags to the quotes they filled\n"
"  report     aggregates: daily swaps, per chain, per pair, size bands, makers\n"
"  settings   print the effective settings and where they came from\n"
"  selftest   built-in tests\n"
"  dump, events, bench   corpus and fuzz tooling for one raw block file\n"
"\n"
"Run `bsx-swap-tracker <command> --help` for that command's flags. Settings: --config FILE, else $BSX_SWAP_TRACKER_CONFIG,\n"
"else deploy/bsx-swap-tracker.toml, else config/bsx-swap-tracker.toml in the checkout, else built-in defaults.\n"
"Outputs go to data_dir (see `bsx-swap-tracker settings`).\n"
"\n"
"A first run:\n"
"  bsx-swap-tracker scan --coins BTC,LTC --probe\n"
"  bsx-swap-tracker scan --coins BTC,LTC --backfill-days 1\n"
"  bsx-swap-tracker report\n";

static const char* help_for(const std::string& cmd) {
    if (cmd == "scan") return
"usage: bsx-swap-tracker scan [flags]\n"
"\n"
"Catch up each chain to its tip, verify every block (proof of work, merkle root), and record swap events in the\n"
"database. Safe to rerun: it continues where it stopped and handles reorgs. BTC and LTC use public peers; PART\n"
"and BCH use your own node.\n"
"\n"
"  --coins LIST          chains to scan, comma separated (default BTC,LTC,BCH,PART)\n"
"  --backfill-days N     how far back to scan, in days, or max (as deep as each source allows, about 180 days and\n"
"                        several GB for Bitcoin). Without it a new database starts 7 days back and an existing one\n"
"                        keeps its depth and only moves forward\n"
"  --probe               only check that each source can be reached; writes nothing\n"
"  --verify N            fetch and fully verify the last N blocks per chain; writes nothing\n"
"  --earliest            print the oldest block each source still serves\n"
"  --dry-run             parse and recognise but write nothing\n"
"  --max-seconds N       time budget of one run (default 3300)\n"
"  --slice-seconds N     time per chain per round, round robin (default 600)\n"
"  --max-blocks N        stop after N blocks per chain (default 0 = no limit)\n"
"  --rps N               max blocks per second per chain from a node (default 3)\n"
"  --no-extend-back      do not scan older ranges that became available\n"
"  --no-wide             do not keep the wide script_spends table\n"
"  --db FILE             database (default <data_dir>/chainscan.sqlite)\n"
"  --data-dir DIR        database and output folder (default: data_dir from settings)\n"
"  --out-dir DIR         status, heartbeat and header files (default: data_dir)\n"
"  --no-lock             skip the single-instance lock\n"
"  --lock-name NAME, --log-name NAME   lock and log names (to run a second instance)\n";
    if (cmd == "watch") return
"usage: bsx-swap-tracker watch [flags]\n"
"\n"
"Live watcher. Holds one connection per chain, reads the mempool and new blocks, and flags likely swap locks the\n"
"moment they appear (early_locks.csv, early_locks_latest.json). Starts a scan after each block and writes\n"
"chainwatch_status.json. Runs until stopped unless --seconds is given.\n"
"\n"
"  --coins LIST          chains to watch (default BTC,LTC,PART,BCH)\n"
"  --seconds N           stop after N seconds and print the status\n"
"  --no-mempool          blocks only\n"
"  --no-trigger          do not start scans after blocks\n"
"  --out-dir DIR         output folder (default: data_dir)\n"
"  --data-dir DIR        override data_dir\n"
"  --snapshots-dir DIR   order-book snapshots, used to tag flags that equal a live quote\n"
"  --lock-name NAME, --log-name NAME   lock and log names\n";
    if (cmd == "snapshot") return
"usage: bsx-swap-tracker snapshot (--url http://127.0.0.1:PORT | --from-jsonl FILE) [flags]\n"
"\n"
"Record the order book from your OWN BasicSwap node. Sends one read-only request (POST /json/offers) to a loopback\n"
"address you name; there is no default URL. Writes <snapshots_dir>/<UTC time>Z.json.gz.\n"
"\n"
"  --url URL             your node's UI address, loopback only, no path\n"
"  --from-jsonl FILE     convert recorded offer lists instead of calling a node\n"
"  --interval S          repeat every S seconds (at least 15; default: once)\n"
"  --count N             number of snapshots with --interval (default 1, 0 = until stopped)\n"
"  --snapshots-dir DIR   output folder (default: snapshots_dir from settings)\n";
    if (cmd == "match") return
"usage: bsx-swap-tracker match [flags]\n"
"\n"
"Tie completed swaps and refunds (BTC, LTC, PART) to the public quote whose full amount equals the lock and that\n"
"was listed at the time, and give each early flag its current status. Needs order-book snapshots (see snapshot).\n"
"Writes offer_match.csv and offer_match_early.csv.\n"
"\n"
"  --days N              match swaps from the last N days (default 2)\n"
"  --context-days N      extra days of snapshots before that window (default 2)\n"
"  --db FILE             database (default <data_dir>/chainscan.sqlite)\n"
"  --out-dir DIR         output folder (default: data_dir)\n"
"  --data-dir DIR        override data_dir\n"
"  --snapshots-dir DIR   override snapshots_dir\n";
    if (cmd == "report") return
"usage: bsx-swap-tracker report [flags]\n"
"\n"
"Aggregates of the database and the offer match: daily_swaps.csv, chains.csv, pairs.csv, sizes.csv, makers.csv.\n"
"Without a match file the pair and maker columns stay empty.\n"
"\n"
"  --public              publication rules: rounding, minimum counts, no makers.csv, today left out\n"
"  --db FILE             database (default <data_dir>/chainscan.sqlite)\n"
"  --match FILE          offer match (default <data_dir>/offer_match.csv)\n"
"  --out-dir DIR         output folder (default <data_dir>/report)\n"
"  --data-dir DIR        override data_dir\n";
    if (cmd == "settings") return "usage: bsx-swap-tracker settings\n\nPrint the effective settings: which file they came from, every path, protected ports, unit names.\n";
    if (cmd == "selftest") return "usage: bsx-swap-tracker selftest\n\nRun the built-in tests (hashes, parsers, output types, JSON, tier rules).\n";
    if (cmd == "dump" || cmd == "events" || cmd == "bench") return
"usage: bsx-swap-tracker dump <btc|ltc|bch|part> <raw block file>     canonical parser output\n"
"       bsx-swap-tracker events <btc|ltc|bch|part> <raw block file>   recognised swap events\n"
"       bsx-swap-tracker bench <btc|ltc|bch|part> <raw block file> [repeats]   parse speed\n";
    return nullptr;
}

static bool is_help(const std::string& a) { return a == "--help" || a == "-h" || a == "help"; }

static Bytes read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open " + path);
    return Bytes(std::istreambuf_iterator<char>(f), {});
}

static std::string hx(const Bytes& b) { return b.empty() ? "-" : hex(b); }

static void dump(const Bytes& raw, Family f, std::string& out) {
    Block b = parse_block(raw, f);
    Hash32 bh = sha256d(View(raw.data(), header_len(f)));
    Hash32 mr = merkle_root(b.txs);
    bool ok = std::equal(mr.begin(), mr.end(), raw.begin() + 36);
    out += "block " + hex_rev(bh) + " fam=" + family_name(f) + " txs=" + std::to_string(b.txs.size()) + " merkle=" + (ok ? "ok" : "BAD") + "\n";
    for (auto& t : b.txs) {
        out += "tx " + hex_rev(t.txid) + " ver=" + std::to_string(t.version) + " lt=" + std::to_string(t.locktime) + " cb=" + (t.coinbase ? "1" : "0") + "\n";
        for (auto& in : t.vin) {
            out += "in ";
            out += in.coinbase ? std::string("-") : hex_rev(in.prev);
            out += " " + std::to_string(in.vout) + " " + std::to_string(in.sequence) + " " + hx(in.script_sig) + " w=";
            if (in.witness.empty()) out += "-";
            for (size_t i = 0; i < in.witness.size(); i++) { if (i) out += ","; out += hex(in.witness[i]); }
            out += "\n";
        }
        for (auto& o : t.vout) {
            out += "out ";
            out += o.value ? std::to_string(*o.value) : std::string("-");
            out += " ";
            out += out_type(o.script).type;
            out += " " + hx(o.script) + "\n";
        }
    }
}

static int selftest() {
    int fails = 0;
    auto check = [&](bool c, const char* what) { if (!c) { std::fprintf(stderr, "FAIL %s\n", what); fails++; } };
    auto h = [](const std::string& s) { return hex(sha256(View(reinterpret_cast<const uint8_t*>(s.data()), s.size()))); };
    check(h("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "sha256 empty");
    check(h("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256 abc");
    check(h("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") == "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1", "sha256 448 bits");
    std::string m(1000000, 'a');
    check(h(m) == "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", "sha256 million a");
    {   // sha256d("abc"), known value
        const uint8_t abc[3] = {'a', 'b', 'c'};
        check(hex(sha256d(View(abc, 3))) == "4f8b42c22dd3729b519ba6f68d2da7cc5b2d606d05daed5ad5128cc03e6c6358", "sha256d abc");
    }
    {   // varint and bounds
        const uint8_t v[] = {0xFD, 0x34, 0x12, 0xFE, 1, 0, 0, 0, 0x05};
        Reader r(View(v, sizeof v));
        check(r.varint() == 0x1234, "varint fd");
        check(r.varint() == 1, "varint fe");
        check(r.varint() == 5, "varint small");
        bool threw = false;
        try { r.u8(); } catch (const ParseError&) { threw = true; }
        check(threw, "read past end throws");
        const uint8_t big[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F};
        Reader r2(View(big, sizeof big));
        threw = false;
        try { r2.varbytes(); } catch (const ParseError&) { threw = true; }
        check(threw, "huge varbytes throws");
    }
    {   // output types
        Bytes p2wsh(34, 0x11); p2wsh[0] = 0x00; p2wsh[1] = 0x20;
        Bytes p2sh32(35, 0x22); p2sh32[0] = 0xAA; p2sh32[1] = 0x20; p2sh32[34] = 0x87;
        check(std::string(out_type(p2wsh).type) == "p2wsh", "out_type p2wsh");
        check(std::string(out_type(p2sh32).type) == "p2sh32", "out_type p2sh32");
        check(std::string(out_type(Bytes{0x6A, 0x01, 0x02}).type) == "opreturn", "out_type opreturn");
        check(std::string(out_type(Bytes{0x51}).type) == "other", "out_type other");
    }
    {   // strong tier needs lock nLockTime 0 and spend sequence 0 (a height-based lock time was tested 2026-11-20 and not adopted)
        Bytes lock(71, 0x02);
        lock[0] = 0x52; lock[1] = 0x21; lock[35] = 0x21; lock[69] = 0x52; lock[70] = 0xAE;
        auto run = [&](int64_t lt, uint32_t seq) {
            Tx t;
            t.version = 2;
            TxIn in;
            in.prev[0] = 7;
            in.sequence = seq;
            in.witness = {Bytes{}, Bytes(71, 0x30), Bytes(71, 0x30), lock};
            t.vin.push_back(in);
            TxOut o;
            o.value = 1000;
            o.script = Bytes{0x00, 0x14, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20};
            t.vout.push_back(o);
            Lookup lk = [&](const std::string&, uint32_t) -> std::optional<PrevOut> {
                PrevOut p;
                p.value = 2000;
                p.height = 100;
                p.info = Json::object();
                p.info.set("lt", Json::integer(lt));
                return p;
            };
            auto r = scan_block(Family::btc, 105, 0, std::vector<Tx>{t}, lk, false, 300, false);
            return r.events.empty() ? std::string("none") : r.events[0].confidence;
        };
        check(run(0, 0) == "strong", "lock lt 0, seq 0 -> strong");
        check(run(98, 0) == "candidate", "lock lt 2 below its height, seq 0 -> candidate (not adopted)");
        check(run(840000, 0) == "candidate", "lock lt above its height -> candidate");
        check(run(98, 0xFFFFFFFF) == "candidate", "recent lock lt, non-zero sequence -> candidate");
        check(run(0, 0xFFFFFFFF) == "likely", "lock lt 0, non-zero sequence -> likely");
    }
    {   // Particl blind swaps (2026-11-20): [data, blinded dest, blinded change], values hidden; same rule as chainscan_lib.blind_outputs
        Bytes lock(71, 0x02);
        lock[0] = 0x52; lock[1] = 0x21; lock[35] = 0x21; lock[69] = 0x52; lock[70] = 0xAE;
        Bytes p2pkh = {0x76, 0xA9, 0x14, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 0x88, 0xAC};
        Bytes p2wsh(34, 0x09); p2wsh[0] = 0x00; p2wsh[1] = 0x20;
        auto run = [&](uint32_t seq, std::vector<std::pair<int, Bytes>> outs, int64_t version) {
            Tx t;
            t.version = version;
            TxIn in;
            in.prev[0] = 7;
            in.sequence = seq;
            in.witness = {Bytes{}, Bytes(71, 0x30), Bytes(71, 0x30), lock};
            t.vin.push_back(in);
            for (auto& [pt, sc] : outs) { TxOut o; o.ptype = pt; o.script = sc; if (pt == 1) o.value = 5; t.vout.push_back(o); }
            Lookup lk = [&](const std::string&, uint32_t) -> std::optional<PrevOut> {
                PrevOut p; p.height = 100; p.info = Json::object(); p.info.set("lt", Json::integer(0)); return p;
            };
            auto r = scan_block(Family::part, 105, 0, std::vector<Tx>{t}, lk, false, 300, false);
            return r.events.empty() ? std::string("none") : r.events[0].kind + ":" + r.events[0].confidence;
        };
        check(run(0, {{4, {}}, {2, p2pkh}, {2, p2pkh}}, 0xA0) == "adaptor_lock_spend:strong", "blind lock spend -> strong");
        check(run(10, {{4, {}}, {2, p2wsh}, {2, p2pkh}}, 0xA0) == "adaptor_refund_tx:likely", "blind refund tx -> refund likely");
        check(run(0, {{4, {}}, {2, p2pkh}, {1, p2pkh}}, 0xA0) == "adaptor_lock_spend:candidate", "blind + visible output -> candidate");
        check(run(0, {{4, {}}, {4, {}}, {2, p2pkh}}, 0xA0) == "adaptor_lock_spend:candidate", "two data outputs -> candidate");
        check(run(0, {{4, {}}, {2, p2pkh}, {2, p2pkh}}, 2) == "adaptor_lock_spend:candidate", "blind shape, wrong version -> candidate");
        check(run(0xFFFFFFFF, {{4, {}}, {2, p2pkh}, {2, p2pkh}}, 0xA0) == "adaptor_lock_spend:likely", "blind, non-zero sequence -> likely");
    }
    {   // refund output scripts: 0.18+ (IF 1 CSV DROP 2 .. ) and the older form without "1 CSV DROP" (missed until 2026-11-20)
        auto refund_kind = [&](bool old) {
            Bytes s;
            if (old) s = {0x63};
            else s = {0x63, 0x51, 0xB2, 0x75};
            s.push_back(0x52); s.push_back(0x21); s.insert(s.end(), 33, 0x02); s.push_back(0x21); s.insert(s.end(), 33, 0x03);
            s.push_back(0x52); s.push_back(0xAE); s.push_back(0x67); s.push_back(0x5A); s.push_back(0xB2); s.push_back(0x75);
            s.push_back(0x21); s.insert(s.end(), 33, 0x03); s.push_back(0xAC); s.push_back(0x68);
            Tx t;
            t.version = 2;
            TxIn in;
            in.prev[0] = 9;
            in.sequence = 10;
            in.witness = {Bytes{}, Bytes(71, 0x30), s};
            t.vin.push_back(in);
            TxOut o;
            o.value = 900;
            o.script = Bytes{0x00, 0x14, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20};
            t.vout.push_back(o);
            Lookup lk = [&](const std::string&, uint32_t) -> std::optional<PrevOut> { PrevOut p; p.value = 1000; p.height = 100; return p; };
            auto r = scan_block(Family::btc, 105, 0, std::vector<Tx>{t}, lk, false, 300, false);
            if (r.events.empty()) return std::string("none");
            const Json* f = r.events[0].extra.get("form");
            return r.events[0].kind + (f ? "/" + f->as_str() : std::string());
        };
        check(refund_kind(false) == "adaptor_refund_spend", "refund script 0.18+");
        check(refund_kind(true) == "adaptor_refund_spend/pre_0_18", "refund script before 0.18");
    }
#ifdef BSX_TEST_DIR
    {   // header sync against simulated peers with real headers: empty start, extension, a stalled peer must not cut the chain back
        std::string why = header_sync_selftest(std::string(BSX_TEST_DIR) + "/btc_headers_950401_300.bin");
        check(why.empty(), ("header sync: " + why).c_str());
    }
#endif
    {   // JSON writer = Python json.dumps (ensure_ascii): code points, surrogate pairs, floats (regression of the fuzz_json finding, 2026-11-19)
        std::string in = "[1,2.5e10,-3,\"\\u00e9\\ud83d\\ude00\",true,false,null,{\"a\":[{}]}]";
        check(json_dump(json_parse(in)) == "[1, 25000000000.0, -3, \"\\u00e9\\ud83d\\ude00\", true, false, null, {\"a\": [{}]}]", "json dump = python");
    }
    std::printf(fails ? "selftest: %d FAILED\n" : "selftest ok\n", fails);
    return fails ? 1 : 0;
}

int main(int argc, char** argv) {
    std::string cmd;
    try {
        // --config FILE (anywhere on the command line) selects the settings file; removed before the subcommand sees its flags
        std::vector<char*> args;
        std::string config;
        for (int i = 0; i < argc; i++) {
            if (std::string(argv[i]) == "--config" && i + 1 < argc) { config = argv[++i]; continue; }
            args.push_back(argv[i]);
        }
        load_settings(config);
        argc = int(args.size());
        args.push_back(nullptr);
        argv = args.data();
        cmd = argc > 1 ? argv[1] : "";
        if (cmd.empty()) { std::fputs(HELP_MAIN, stderr); return 2; }
        if (is_help(cmd)) {
            const char* h = argc > 2 ? help_for(argv[2]) : nullptr;
            std::fputs(h ? h : HELP_MAIN, stdout);
            return 0;
        }
        for (int i = 2; i < argc; i++)
            if (std::string(argv[i]) == "--help" || std::string(argv[i]) == "-h") {
                const char* h = help_for(cmd);
                if (!h) break;
                std::fputs(h, stdout);
                return 0;
            }
        if (cmd == "selftest") return selftest();
        if (cmd == "scan") return scan_main(argc, argv);
        if (cmd == "settings") {                           // print the effective settings (which file, every path, protected ports)
            const Settings& s = settings();
            std::printf("source               %s\nroot                 %s\ndata_dir             %s\nsnapshots_dir        %s\nlogs_dir             %s\nlocks_dir            %s\n"
                        "basicswap_json       %s\nelectrum_headers_dir %s\nprotected_ports     ", s.source.c_str(), s.root.c_str(), s.data_dir.c_str(), s.snapshots_dir.c_str(),
                        s.logs_dir.c_str(), s.locks_dir.c_str(), s.basicswap_json.c_str(), s.electrum_headers_dir.c_str());
            for (int p : s.protected_ports) std::printf(" %d", p);
            std::printf("\nscan_units          ");
            for (auto& [c, u] : s.scan_units) std::printf(" %s=%s", c.c_str(), u.c_str());
            std::printf("\nmatch_unit           %s\n", s.match_unit.c_str());
            return 0;
        }
        if (cmd == "watch") return watch_main(argc, argv);
        if (cmd == "match") return match_main(argc, argv);
        if (cmd == "snapshot") return snapshot_main(argc, argv);
        if (cmd == "report") return report_main(argc, argv);
        if (cmd == "dump" && argc == 4) {
            Bytes raw = read_file(argv[3]);
            std::string out;
            dump(raw, family_from(argv[2]), out);
            std::fwrite(out.data(), 1, out.size(), stdout);
            return 0;
        }
        if (cmd == "events" && argc == 4) {                 // scan_block events without an output index (corpus .events.txt format)
            Bytes raw = read_file(argv[3]);
            Family f = family_from(argv[2]);
            Block b = parse_block(raw, f);
            ScanResult r = scan_block(f, 0, b.time, b.txs, nullptr, true, 300, false);
            std::string out;
            for (auto& e : r.events)
                out += "ev " + e.kind + " " + e.confidence + " " + e.txid + " " + std::to_string(e.vin) + " " + (e.amount ? std::to_string(*e.amount) : std::string("-")) + "\n";
            std::fwrite(out.data(), 1, out.size(), stdout);
            return 0;
        }
        if (cmd == "bench" && argc >= 4) {
            Bytes raw = read_file(argv[3]);
            int reps = argc > 4 ? std::stoi(argv[4]) : 20;
            Family f = family_from(argv[2]);
            size_t ntx = 0;
            auto t0 = std::chrono::steady_clock::now();
            for (int i = 0; i < reps; i++) { Block b = parse_block(raw, f); Hash32 m = merkle_root(b.txs); ntx += b.txs.size() + m[0] * 0; }
            double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            std::printf("parsed + merkle %d x %zu bytes (%zu txs each) in %.3f s: %.2f ms per block\n", reps, raw.size(), ntx / reps, s, 1000 * s / reps);
            return 0;
        }
        if (help_for(cmd)) std::fprintf(stderr, "%s: wrong arguments\n\n%s", cmd.c_str(), help_for(cmd));
        else std::fprintf(stderr, "unknown command '%s'\n\n%s", cmd.c_str(), HELP_MAIN);
        return 2;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        if (std::string(e.what()).rfind("unknown flag", 0) == 0 && help_for(cmd))
            std::fprintf(stderr, "run `bsx-swap-tracker %s --help` for the flags\n", cmd.c_str());
        return 1;
    }
}
