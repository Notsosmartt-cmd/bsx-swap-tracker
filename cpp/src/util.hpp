// Paths, logging (same line format as python/collectors/common.setup_logging), the single-instance lock protocol of python/collectors/common.single_instance
// (data/locks/<name>.json with host, pid, updated), heartbeat files, atomic writes, time helpers.
#pragma once
#include <atomic>
#include <cstdint>
#include <string>

namespace bsx {

extern std::atomic<bool> g_stop;                 // set by SIGTERM / SIGINT: loops end at a block boundary

std::string root_dir();                          // repo root: $BSX_ROOT, else the parent of the binary's bin/ folder
std::string data_dir();                          // <root>/data
double now_s();
int64_t now_i();
std::string utc_iso(int64_t t);                  // 2026-11-19T21:22:29Z
std::string utc_date(int64_t t);                 // 2026-11-19

void log_open(const std::string& name);          // data/logs/<name>.log (append) + stderr
void log_line(const char* level, const std::string& msg);
#define LOG_INFO(m) ::bsx::log_line("INFO", (m))
#define LOG_WARN(m) ::bsx::log_line("WARNING", (m))
#define LOG_ERROR(m) ::bsx::log_line("ERROR", (m))

bool single_instance(const std::string& name);   // false = another live instance holds the lock (caller exits quietly)
void heartbeat(const std::string& path, const std::string& coin, int64_t height, const std::string& note);   // at most every 10 s
bool write_atomic(const std::string& path, const std::string& content);
std::string read_text(const std::string& path);  // throws on error
void make_dirs(const std::string& path);
std::string fmt(const char* f, ...) __attribute__((format(printf, 1, 2)));

}  // namespace bsx
