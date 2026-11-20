// SQLite store with the schema and write semantics of chain_swap_scan.py (open_db, insert_block, make_lookup, rewind, finalize_run, coverage,
// write_status, db_retry). Same file format, so Python readers (offer match, export, reports) need no change.
#pragma once
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <sqlite3.h>
#include <string>
#include <vector>
#include "json.hpp"
#include "scan.hpp"

namespace bsx {

struct DbBusy : std::runtime_error { using std::runtime_error::runtime_error; };

class Stmt {
public:
    Stmt(sqlite3* db, const std::string& sql);
    ~Stmt();
    Stmt& bind(int i, int64_t v);
    Stmt& bind(int i, const std::string& v);
    Stmt& bind_null(int i);
    template <class T> Stmt& bind_opt(int i, const std::optional<T>& v) { return v ? bind(i, int64_t(*v)) : bind_null(i); }
    Stmt& bind_opt(int i, const std::optional<std::string>& v) { return v ? bind(i, *v) : bind_null(i); }
    bool step();                                   // true = row available
    void run() { while (step()) {} reset(); }
    void reset();
    int64_t col_int(int i);
    bool col_null(int i);
    std::string col_str(int i);

private:
    sqlite3* db_;
    sqlite3_stmt* st_ = nullptr;
};

class Store {
public:
    explicit Store(const std::string& path);
    ~Store();
    sqlite3* raw() { return db_; }
    void exec(const std::string& sql);
    void begin();
    void commit();
    void rollback();
    void retry(const std::function<void()>& fn, int tries = 40);      // db_retry: redo the whole unit of work while another writer holds the lock

    std::optional<PrevOut> lookup(const std::string& coin, const std::string& txid, uint32_t vout);
    void insert_p2sh(const std::string& coin, const std::vector<P2shRow>& rows);
    void insert_block(const std::string& coin, int64_t height, const std::string& hash, int64_t time, size_t ntx, const std::string& prev, const ScanResult& r);
    void rewind(const std::string& coin, int64_t from_height);
    std::optional<int64_t> max_height(const std::string& coin);
    std::optional<int64_t> min_height(const std::string& coin);
    std::vector<std::pair<int64_t, std::string>> last_blocks(const std::string& coin, int n);
    bool has_state(const std::string& coin);
    void add_gap(const std::string& coin, int64_t h, const std::string& reason);
    std::optional<std::pair<int64_t, int64_t>> backfill(const std::string& coin);
    void set_backfill(const std::string& coin, int64_t next_h, int64_t end_h);
    void backfill_next(const std::string& coin, int64_t next_h);
    void drop_backfill(const std::string& coin);
    int64_t p2sh_count(const std::string& coin, int64_t lo, int64_t hi);
    void finalize_run(const std::string& coin, int64_t tip, int bpd, int retain_days);
    Json coverage(const std::string& coin);
    void write_status(const std::string& path, const std::map<std::string, std::string>& states);

private:
    sqlite3* db_ = nullptr;
    std::unique_ptr<Stmt> lookup_st_;          // finalized before the connection closes
};

}  // namespace bsx
