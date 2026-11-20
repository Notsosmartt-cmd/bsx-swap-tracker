#include "util.hpp"
#include <climits>
#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <mutex>
#include <signal.h>
#include <sstream>
#include <sys/stat.h>
#include <sys/time.h>
#include <thread>
#include <unistd.h>
#include "json.hpp"
#include "settings.hpp"

namespace bsx {

std::atomic<bool> g_stop{false};

static std::string g_root;

std::string root_dir() {
    if (!g_root.empty()) return g_root;
    if (const char* e = getenv("BSX_ROOT")) return g_root = e;
    char buf[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n > 0) {
        buf[n] = 0;
        std::string p(buf);
        // walk up from the binary (<root>/bin/bsx-swap-tracker, <root>/build/bsx-swap-tracker, <root>/cpp/build-release/...) to a folder that holds
        // the Python collectors: python/collectors/ (a larger checkout that embeds the tracker) or python/collectors/ (this layout)
        for (int k = 0; k < 5; k++) {
            auto s = p.find_last_of('/');
            if (s == std::string::npos || s == 0) break;
            p = p.substr(0, s);
            if (access((p + "/collectors").c_str(), F_OK) == 0 || access((p + "/python/collectors").c_str(), F_OK) == 0) return g_root = p;
        }
    }
    char cwd[PATH_MAX];
    return g_root = (getcwd(cwd, sizeof cwd) ? std::string(cwd) : std::string("."));
}

std::string data_dir() { return root_dir() + "/data"; }

double now_s() { struct timeval tv; gettimeofday(&tv, nullptr); return tv.tv_sec + tv.tv_usec / 1e6; }
int64_t now_i() { return int64_t(time(nullptr)); }

std::string utc_iso(int64_t t) {
    time_t tt = t; struct tm g; gmtime_r(&tt, &g);
    char b[32]; strftime(b, sizeof b, "%Y-%m-%dT%H:%M:%SZ", &g);
    return b;
}

std::string utc_date(int64_t t) {
    time_t tt = t; struct tm g; gmtime_r(&tt, &g);
    char b[16]; strftime(b, sizeof b, "%Y-%m-%d", &g);
    return b;
}

std::string fmt(const char* f, ...) {
    char b[4096];
    va_list ap;
    va_start(ap, f);
    vsnprintf(b, sizeof b, f, ap);
    va_end(ap);
    return b;
}

static std::mutex g_log_mx;
static FILE* g_log = nullptr;

void make_dirs(const std::string& path) {
    std::string cur;
    std::stringstream ss(path);
    std::string part;
    if (!path.empty() && path[0] == '/') cur = "/";
    while (std::getline(ss, part, '/')) {
        if (part.empty()) continue;
        cur += part + "/";
        mkdir(cur.c_str(), 0700);
    }
}

void log_open(const std::string& name) {
    make_dirs(settings().logs_dir);
    g_log = fopen((settings().logs_dir + "/" + name + ".log").c_str(), "a");
}

void log_line(const char* level, const std::string& msg) {
    struct timeval tv; gettimeofday(&tv, nullptr);
    time_t t = tv.tv_sec; struct tm l; localtime_r(&t, &l);
    char ts[32]; strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &l);
    char thread[32];
    if (pthread_getname_np(pthread_self(), thread, sizeof thread) != 0) snprintf(thread, sizeof thread, "?");
    std::string line = fmt("%s,%03d %s [%s] %s\n", ts, int(tv.tv_usec / 1000), level, thread, msg.c_str());
    std::lock_guard<std::mutex> g(g_log_mx);
    if (g_log) { fputs(line.c_str(), g_log); fflush(g_log); }
    fputs(line.c_str(), stderr);
}

std::string read_text(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open " + path);
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

bool write_atomic(const std::string& path, const std::string& content) {
    std::string tmp = path + "." + std::to_string(getpid()) + ".tmp";
    FILE* f = fopen(tmp.c_str(), "w");
    if (!f) return false;
    bool ok = fwrite(content.data(), 1, content.size(), f) == content.size();
    ok = (fclose(f) == 0) && ok;
    if (!ok || rename(tmp.c_str(), path.c_str()) != 0) { unlink(tmp.c_str()); return false; }
    return true;
}

static std::string hostname() { char b[256] = {0}; gethostname(b, sizeof b - 1); return b; }

bool single_instance(const std::string& name) {
    std::string dir = settings().locks_dir;
    make_dirs(dir);
    std::string path = dir + "/" + name + ".json";
    std::string host = hostname();
    int pid = getpid();
    try {
        Json cur = json_parse(read_text(path));
        double updated = 0;
        if (const Json* u = cur.get("updated")) updated = u->t == Json::T::Num ? u->d : double(u->as_int());
        const Json* h = cur.get("host");
        const Json* p = cur.get("pid");
        bool same_host = h && h->as_str() == host;
        int other = p ? int(p->as_int()) : -1;
        bool dead_local = same_host && other > 0 && kill(other, 0) != 0 && errno == ESRCH;
        if (!(same_host && other == pid) && now_s() - updated < 75 && !dead_local) {
            fprintf(stderr, "%s: another instance holds %s (%s pid %d) - exiting\n", name.c_str(), path.c_str(), h ? h->as_str().c_str() : "?", other);
            return false;
        }
    } catch (...) {
    }
    std::thread([path, host, pid]() {
        pthread_setname_np(pthread_self(), "instance-lock");
        while (true) {
            Json j = Json::object();
            j.set("host", Json::str(host));
            j.set("pid", Json::integer(pid));
            j.set("updated", Json::number(now_s()));
            j.set("updated_iso", Json::str(utc_iso(now_i())));
            j.set("impl", Json::str("c++"));
            write_atomic(path, json_dump(j));
            for (int k = 0; k < 20 && !g_stop; k++) sleep(1);
            if (g_stop) {
                try { Json c = json_parse(read_text(path)); if (c.get("pid") && c.get("pid")->as_int() == pid) unlink(path.c_str()); } catch (...) {}
                return;
            }
        }
    }).detach();
    return true;
}

void heartbeat(const std::string& path, const std::string& coin, int64_t height, const std::string& note) {
    static double last = 0;
    if (path.empty() || now_s() - last < 10) return;
    last = now_s();
    Json j = Json::object();
    j.set("ts", Json::integer(int64_t(last)));
    j.set("pid", Json::integer(getpid()));
    j.set("coin", coin.empty() ? Json::null() : Json::str(coin));
    j.set("height", height >= 0 ? Json::integer(height) : Json::null());
    j.set("note", Json::str(note));
    j.set("impl", Json::str("c++"));
    write_atomic(path, json_dump(j));
}

}  // namespace bsx
