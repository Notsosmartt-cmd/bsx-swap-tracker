#include "store.hpp"
#include <chrono>
#include <cmath>
#include <memory>
#include <map>
#include <set>
#include <thread>
#include "util.hpp"

namespace bsx {

static const char* SCHEMA = R"SQL(
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS scan_state(coin TEXT PRIMARY KEY, last_height INTEGER, last_hash TEXT, last_time INTEGER, first_height INTEGER, state TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS blocks(coin TEXT, height INTEGER, hash TEXT, time INTEGER, ntx INTEGER, prev TEXT, PRIMARY KEY(coin, height));
CREATE TABLE IF NOT EXISTS p2sh_outputs(coin TEXT, txid TEXT, vout INTEGER, value INTEGER, otype TEXT, program TEXT, height INTEGER, time INTEGER, txinfo TEXT, PRIMARY KEY(coin, txid, vout));
CREATE INDEX IF NOT EXISTS p2sh_height ON p2sh_outputs(coin, height);
CREATE TABLE IF NOT EXISTS script_spends(coin TEXT, height INTEGER, time INTEGER, txid TEXT, vin INTEGER, script_sha256 TEXT, script_len INTEGER, script_hex TEXT,
  nwit INTEGER, nin INTEGER, nout INTEGER, prev_txid TEXT, prev_vout INTEGER, prev_value INTEGER, PRIMARY KEY(coin, txid, vin));
CREATE INDEX IF NOT EXISTS spends_sha ON script_spends(script_sha256);
CREATE TABLE IF NOT EXISTS swap_events(id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT, height INTEGER, block_hash TEXT, block_time INTEGER, txid TEXT, vin INTEGER,
  kind TEXT, protocol TEXT, confidence TEXT, secret_hash TEXT, secret TEXT, lock_val INTEGER, script_sha256 TEXT, amount_sat INTEGER, prev_txid TEXT, prev_vout INTEGER,
  sequence INTEGER, tx_nin INTEGER, tx_nout INTEGER, tx_version INTEGER, tx_locktime INTEGER, outputs TEXT, lock_tx TEXT, lock_height INTEGER, lock_time INTEGER, extra TEXT,
  UNIQUE(coin, txid, vin, kind));
CREATE INDEX IF NOT EXISTS events_hash ON swap_events(secret_hash);
CREATE INDEX IF NOT EXISTS events_time ON swap_events(coin, block_time);
CREATE TABLE IF NOT EXISTS backfill(coin TEXT PRIMARY KEY, next_h INTEGER, end_h INTEGER);
CREATE TABLE IF NOT EXISTS gaps(coin TEXT, from_height INTEGER, to_height INTEGER, reason TEXT, ts INTEGER);
)SQL";

static void chk(sqlite3* db, int rc, const char* what) {
    if (rc == SQLITE_OK || rc == SQLITE_ROW || rc == SQLITE_DONE) return;
    std::string m = std::string(what) + ": " + sqlite3_errmsg(db);
    if (rc == SQLITE_BUSY || rc == SQLITE_LOCKED || (rc & 0xFF) == SQLITE_BUSY || (rc & 0xFF) == SQLITE_LOCKED) throw DbBusy(m);
    throw std::runtime_error(m);
}

Stmt::Stmt(sqlite3* db, const std::string& sql) : db_(db) { chk(db, sqlite3_prepare_v2(db, sql.c_str(), -1, &st_, nullptr), "prepare"); }
Stmt::~Stmt() { if (st_) sqlite3_finalize(st_); }
Stmt& Stmt::bind(int i, int64_t v) { chk(db_, sqlite3_bind_int64(st_, i, v), "bind"); return *this; }
Stmt& Stmt::bind(int i, const std::string& v) { chk(db_, sqlite3_bind_text(st_, i, v.data(), int(v.size()), SQLITE_TRANSIENT), "bind"); return *this; }
Stmt& Stmt::bind_null(int i) { chk(db_, sqlite3_bind_null(st_, i), "bind"); return *this; }
bool Stmt::step() {
    int rc = sqlite3_step(st_);
    if (rc == SQLITE_ROW) return true;
    if (rc == SQLITE_DONE) return false;
    sqlite3_reset(st_);
    chk(db_, rc, "step");
    return false;
}
void Stmt::reset() { sqlite3_reset(st_); sqlite3_clear_bindings(st_); }
int64_t Stmt::col_int(int i) { return sqlite3_column_int64(st_, i); }
bool Stmt::col_null(int i) { return sqlite3_column_type(st_, i) == SQLITE_NULL; }
std::string Stmt::col_str(int i) { auto p = sqlite3_column_text(st_, i); return p ? std::string(reinterpret_cast<const char*>(p), size_t(sqlite3_column_bytes(st_, i))) : std::string(); }

Store::Store(const std::string& path) {
    auto slash = path.find_last_of('/');
    if (slash != std::string::npos) make_dirs(path.substr(0, slash));
    chk(db_, sqlite3_open(path.c_str(), &db_), "open");
    sqlite3_busy_timeout(db_, 180000);
    exec(SCHEMA);
    exec("PRAGMA synchronous=NORMAL");
    exec("PRAGMA cache_size=-65536");
    exec("PRAGMA wal_autocheckpoint=20000");
}

Store::~Store() {
    lookup_st_.reset();
    if (db_) sqlite3_close_v2(db_);
}

void Store::exec(const std::string& sql) {
    char* err = nullptr;
    int rc = sqlite3_exec(db_, sql.c_str(), nullptr, nullptr, &err);
    std::string m = err ? err : "";
    sqlite3_free(err);
    if (rc != SQLITE_OK) {
        if (rc == SQLITE_BUSY || rc == SQLITE_LOCKED) throw DbBusy(m);
        throw std::runtime_error("sqlite: " + m);
    }
}

void Store::begin() { if (sqlite3_get_autocommit(db_)) exec("BEGIN"); }
void Store::commit() { if (!sqlite3_get_autocommit(db_)) exec("COMMIT"); }
void Store::rollback() { if (!sqlite3_get_autocommit(db_)) sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr); }

void Store::retry(const std::function<void()>& fn, int tries) {
    for (int a = 0; a < tries; a++) {
        try { fn(); return; }
        catch (const DbBusy&) { rollback(); std::this_thread::sleep_for(std::chrono::milliseconds(int(1000 * std::min(10.0, 0.5 * (a + 1))))); }
    }
    throw std::runtime_error("database stayed locked");
}

std::optional<PrevOut> Store::lookup(const std::string& coin, const std::string& txid, uint32_t vout) {
    if (!lookup_st_) lookup_st_ = std::make_unique<Stmt>(db_, "SELECT value, otype, program, height, time, txinfo FROM p2sh_outputs WHERE coin=? AND txid=? AND vout=?");
    auto& st = lookup_st_;
    st->reset();
    st->bind(1, coin).bind(2, txid).bind(3, int64_t(vout));
    if (!st->step()) { st->reset(); return std::nullopt; }
    PrevOut p;
    if (!st->col_null(0)) p.value = st->col_int(0);
    p.otype = st->col_str(1);
    p.program = st->col_str(2);
    p.height = st->col_int(3);
    p.time = st->col_int(4);
    if (!st->col_null(5)) { std::string t = st->col_str(5); if (!t.empty()) p.info = json_parse(t); }
    st->reset();
    return p;
}

void Store::insert_p2sh(const std::string& coin, const std::vector<P2shRow>& rows) {
    Stmt st(db_, "INSERT OR REPLACE INTO p2sh_outputs VALUES (?,?,?,?,?,?,?,?,?)");
    for (auto& r : rows) {
        st.bind(1, coin).bind(2, r.txid).bind(3, int64_t(r.vout));
        if (r.value) st.bind(4, int64_t(*r.value)); else st.bind_null(4);
        st.bind(5, r.otype).bind(6, r.program).bind(7, r.height).bind(8, r.time).bind(9, r.txinfo);
        st.run();
    }
}

void Store::insert_block(const std::string& coin, int64_t height, const std::string& hash, int64_t time, size_t ntx, const std::string& prev, const ScanResult& r) {
    Stmt b(db_, "INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?)");
    b.bind(1, coin).bind(2, height).bind(3, hash).bind(4, time).bind(5, int64_t(ntx)).bind(6, prev).run();
    insert_p2sh(coin, r.p2sh);
    Stmt s(db_, "INSERT OR IGNORE INTO script_spends VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
    for (auto& x : r.spends) {
        s.bind(1, coin).bind(2, x.height).bind(3, x.time).bind(4, x.txid).bind(5, int64_t(x.vin)).bind(6, x.script_sha256).bind(7, int64_t(x.script_len));
        s.bind_opt(8, x.script_hex);
        s.bind(9, int64_t(x.nwit)).bind(10, int64_t(x.nin)).bind(11, int64_t(x.nout)).bind(12, x.prev_txid).bind(13, int64_t(x.prev_vout));
        s.bind_opt(14, x.prev_value);
        s.run();
    }
    Stmt e(db_, "INSERT OR IGNORE INTO swap_events(coin,height,block_hash,block_time,txid,vin,kind,protocol,confidence,secret_hash,secret,lock_val,script_sha256,amount_sat,"
                "prev_txid,prev_vout,sequence,tx_nin,tx_nout,tx_version,tx_locktime,outputs,lock_tx,lock_height,lock_time,extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
    Stmt up(db_, "UPDATE swap_events SET confidence='certain', kind='adaptor_refund_tx' WHERE coin=? AND txid=? AND kind IN ('adaptor_refund_tx','adaptor_lock_spend')");
    for (auto& v : r.events) {
        e.bind(1, coin).bind(2, height).bind(3, hash).bind(4, time).bind(5, v.txid).bind(6, int64_t(v.vin)).bind(7, v.kind).bind(8, v.protocol).bind(9, v.confidence);
        e.bind_opt(10, v.secret_hash).bind_opt(11, v.secret).bind_opt(12, v.lock_val);
        e.bind(13, v.script_sha256).bind_opt(14, v.amount);
        e.bind(15, v.prev_txid).bind(16, int64_t(v.prev_vout)).bind(17, int64_t(v.sequence)).bind(18, int64_t(v.nin)).bind(19, int64_t(v.nout));
        e.bind(20, v.version).bind(21, int64_t(v.locktime)).bind(22, json_dump(v.outputs));
        bool has_lock = v.lock_tx.t == Json::T::Obj && !v.lock_tx.o->empty();
        if (has_lock) e.bind(23, json_dump(v.lock_tx)); else e.bind_null(23);
        e.bind_opt(24, v.lock_height).bind_opt(25, v.lock_time);
        e.bind(26, json_dump(v.extra.t == Json::T::Obj ? v.extra : Json::object()));
        e.run();
        if (v.kind == "adaptor_refund_spend") up.bind(1, coin).bind(2, v.prev_txid).run();
    }
}

void Store::rewind(const std::string& coin, int64_t from) {
    for (const char* t : {"blocks", "p2sh_outputs", "script_spends", "swap_events"}) {
        Stmt d(db_, std::string("DELETE FROM ") + t + " WHERE coin=? AND height>=?");
        d.bind(1, coin).bind(2, from).run();
    }
    commit();
}

std::optional<int64_t> Store::max_height(const std::string& coin) {
    Stmt s(db_, "SELECT MAX(height) FROM blocks WHERE coin=?");
    s.bind(1, coin);
    if (s.step() && !s.col_null(0)) return s.col_int(0);
    return std::nullopt;
}

std::optional<int64_t> Store::min_height(const std::string& coin) {
    Stmt s(db_, "SELECT MIN(height) FROM blocks WHERE coin=?");
    s.bind(1, coin);
    if (s.step() && !s.col_null(0)) return s.col_int(0);
    return std::nullopt;
}

std::vector<std::pair<int64_t, std::string>> Store::last_blocks(const std::string& coin, int n) {
    Stmt s(db_, "SELECT height, hash FROM blocks WHERE coin=? ORDER BY height DESC LIMIT ?");
    s.bind(1, coin).bind(2, int64_t(n));
    std::vector<std::pair<int64_t, std::string>> out;
    while (s.step()) out.emplace_back(s.col_int(0), s.col_str(1));
    return out;
}

bool Store::has_state(const std::string& coin) {
    Stmt s(db_, "SELECT 1 FROM scan_state WHERE coin=?");
    s.bind(1, coin);
    return s.step();
}

void Store::add_gap(const std::string& coin, int64_t h, const std::string& reason) {
    Stmt s(db_, "INSERT INTO gaps VALUES (?,?,?,?,?)");
    s.bind(1, coin).bind(2, h).bind(3, h).bind(4, reason).bind(5, now_i()).run();
}

std::optional<std::pair<int64_t, int64_t>> Store::backfill(const std::string& coin) {
    Stmt s(db_, "SELECT next_h, end_h FROM backfill WHERE coin=?");
    s.bind(1, coin);
    if (s.step()) return std::make_pair(s.col_int(0), s.col_int(1));
    return std::nullopt;
}

void Store::set_backfill(const std::string& coin, int64_t a, int64_t b) { Stmt s(db_, "INSERT OR REPLACE INTO backfill VALUES (?,?,?)"); s.bind(1, coin).bind(2, a).bind(3, b).run(); }
void Store::backfill_next(const std::string& coin, int64_t n) { Stmt s(db_, "UPDATE backfill SET next_h=? WHERE coin=?"); s.bind(1, n).bind(2, coin).run(); }
void Store::drop_backfill(const std::string& coin) { Stmt s(db_, "DELETE FROM backfill WHERE coin=?"); s.bind(1, coin).run(); }

int64_t Store::p2sh_count(const std::string& coin, int64_t lo, int64_t hi) {
    Stmt s(db_, "SELECT COUNT(*) FROM p2sh_outputs WHERE coin=? AND height BETWEEN ? AND ?");
    s.bind(1, coin).bind(2, lo).bind(3, hi);
    return s.step() ? s.col_int(0) : 0;
}

void Store::finalize_run(const std::string& coin, int64_t tip, int bpd, int retain_days) {
    auto last = max_height(coin);
    if (last) {
        Stmt q(db_, "SELECT hash, time FROM blocks WHERE coin=? AND height=?");
        q.bind(1, coin).bind(2, *last);
        q.step();
        std::string lh = q.col_str(0);
        int64_t lt = q.col_int(1);
        auto first = min_height(coin);
        Stmt s(db_, "INSERT OR REPLACE INTO scan_state VALUES (?,?,?,?,?,?,?)");
        s.bind(1, coin).bind(2, *last).bind(3, lh).bind(4, lt).bind(5, first ? *first : 0).bind(6, std::string(*last >= tip ? "caught_up" : "partial")).bind(7, now_i()).run();
    }
    int64_t keep = int64_t(bpd) * retain_days;
    std::vector<std::pair<int64_t, int64_t>> wins;
    if (last) wins.emplace_back(*last - keep, *last);
    if (auto bf = backfill(coin)) wins.emplace_back(bf->first - keep, bf->first);
    std::string q = "DELETE FROM p2sh_outputs WHERE coin=? AND height < ?";
    for (size_t k = 0; k < wins.size(); k++) q += " AND NOT (height BETWEEN ? AND ?)";
    Stmt d(db_, q);
    d.bind(1, coin).bind(2, tip - keep);
    int i = 3;
    for (auto& w : wins) { d.bind(i++, w.first); d.bind(i++, w.second); }
    d.run();
    commit();
}

Json Store::coverage(const std::string& coin) {
    Json c = Json::object();
    Stmt s(db_, "SELECT COUNT(*), MIN(height), MAX(height), MAX(time) FROM blocks WHERE coin=?");
    s.bind(1, coin);
    s.step();
    int64_t n = s.col_int(0);
    if (!n) { c.set("stored", Json::integer(0)); c.set("complete", Json::boolean(false)); return c; }
    int64_t lo = s.col_int(1), hi = s.col_int(2), t1 = s.col_int(3);
    Stmt t(db_, "SELECT time FROM blocks WHERE coin=? AND height=?");
    t.bind(1, coin).bind(2, lo);
    t.step();
    int64_t t0 = t.col_int(0);
    int64_t missing = (hi - lo + 1) - n;
    auto bf = backfill(coin);
    int64_t pending = (bf && bf->first <= bf->second) ? bf->second - bf->first + 1 : 0;
    Stmt g(db_, "SELECT COUNT(*) FROM gaps WHERE coin=?");
    g.bind(1, coin);
    g.step();
    int64_t gaps = g.col_int(0);
    double lag_h = (now_s() - double(t1)) / 3600;
    c.set("stored", Json::integer(n));
    c.set("first_height", Json::integer(lo));
    c.set("last_height", Json::integer(hi));
    c.set("days_spanned", Json::number(std::round((t1 - t0) / 86400.0 * 10) / 10));
    c.set("missing_inside_range", Json::integer(missing));
    c.set("older_range_blocks_pending", Json::integer(pending));
    c.set("gaps", Json::integer(gaps));
    c.set("newest_block_age_h", Json::number(std::round(lag_h * 10) / 10));
    c.set("oldest_block_utc", Json::str(utc_date(t0)));
    c.set("complete", Json::boolean(missing == 0 && pending == 0 && gaps == 0 && lag_h <= 3.0));
    return c;
}

void Store::write_status(const std::string& path, const std::map<std::string, std::string>& states) {
    static const std::map<std::string, std::string> retired = {{"DASH", "never holds the script lock (no segwit)"}, {"DOGE", "never holds the script lock (scriptless side)"},
                                                               {"DCR", "no DCR offers on the board; events are Decred DEX"}};
    std::set<std::string> coins = {"BTC", "LTC", "BCH", "PART"};
    { Stmt s(db_, "SELECT coin FROM scan_state"); while (s.step()) coins.insert(s.col_str(0)); }
    Json st = Json::object();
    st.set("updated", Json::str(utc_iso(now_i())));
    st.set("impl", Json::str("c++"));
    Json cs = Json::object();
    for (auto& coin : coins) {
        Json c = Json::object();
        auto it = states.find(coin);
        c.set("run_state", it != states.end() ? Json::str(it->second) : Json::null());
        Stmt s(db_, "SELECT last_height, last_time, first_height, state FROM scan_state WHERE coin=?");
        s.bind(1, coin);
        bool has = s.step();
        c.set("last_height", has ? Json::integer(s.col_int(0)) : Json::null());
        c.set("first_height", has ? Json::integer(s.col_int(2)) : Json::null());
        c.set("last_block_utc", has && s.col_int(1) ? Json::str(utc_iso(s.col_int(1))) : Json::null());
        Json ev = Json::object();
        Stmt e(db_, "SELECT kind || '/' || confidence, COUNT(*) FROM swap_events WHERE coin=? GROUP BY 1");
        e.bind(1, coin);
        while (e.step()) ev.set(e.col_str(0), Json::integer(e.col_int(1)));
        c.set("events", ev);
        Stmt g(db_, "SELECT COUNT(*) FROM gaps WHERE coin=?");
        g.bind(1, coin);
        g.step();
        c.set("gaps", Json::integer(g.col_int(0)));
        c.set("coverage", coverage(coin));
        auto r = retired.find(coin);
        c.set("retired", r != retired.end() ? Json::str(r->second) : Json::null());
        cs.set(coin, c);
    }
    st.set("coins", cs);
    write_atomic(path, json_dump(st));
}

}  // namespace bsx
