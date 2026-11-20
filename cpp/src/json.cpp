#include "json.hpp"
#include <cmath>
#include <cstdio>
#include <stdexcept>

namespace bsx {

namespace {
struct P {
    const std::string& s;
    size_t i = 0;
    int depth = 0;
    [[noreturn]] void fail(const char* m) { throw std::runtime_error(std::string("json: ") + m + " at " + std::to_string(i)); }
    void ws() { while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\r' || s[i] == '\t')) i++; }
    bool lit(const char* w) {
        size_t n = std::char_traits<char>::length(w);
        if (s.compare(i, n, w) == 0) { i += n; return true; }
        return false;
    }
    static void utf8(std::string& out, uint32_t c) {
        if (c < 0x80) out += char(c);
        else if (c < 0x800) { out += char(0xC0 | (c >> 6)); out += char(0x80 | (c & 63)); }
        else if (c < 0x10000) { out += char(0xE0 | (c >> 12)); out += char(0x80 | ((c >> 6) & 63)); out += char(0x80 | (c & 63)); }
        else { out += char(0xF0 | (c >> 18)); out += char(0x80 | ((c >> 12) & 63)); out += char(0x80 | ((c >> 6) & 63)); out += char(0x80 | (c & 63)); }
    }
    uint32_t hex4() {
        if (i + 4 > s.size()) fail("short \\u");
        uint32_t v = 0;
        for (int k = 0; k < 4; k++) {
            char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= uint32_t(c - '0');
            else if (c >= 'a' && c <= 'f') v |= uint32_t(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= uint32_t(c - 'A' + 10);
            else fail("bad \\u");
        }
        return v;
    }
    std::string string() {
        if (s[i] != '"') fail("expected string");
        i++;
        std::string out;
        while (true) {
            if (i >= s.size()) fail("unterminated string");
            char c = s[i++];
            if (c == '"') break;
            if (c != '\\') { out += c; continue; }
            if (i >= s.size()) fail("bad escape");
            char e = s[i++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    uint32_t c1 = hex4();
                    if (c1 >= 0xD800 && c1 < 0xDC00 && lit("\\u")) { uint32_t c2 = hex4(); c1 = 0x10000 + ((c1 - 0xD800) << 10) + (c2 - 0xDC00); }
                    utf8(out, c1);
                    break;
                }
                default: fail("bad escape");
            }
        }
        return out;
    }
    Json value() {
        if (++depth > 200) fail("too deep");
        ws();
        if (i >= s.size()) fail("unexpected end");
        Json v;
        char c = s[i];
        if (c == '{') {
            v = Json::object(); i++; ws();
            if (i < s.size() && s[i] == '}') { i++; }
            else while (true) {
                ws();
                std::string k = string();
                ws();
                if (i >= s.size() || s[i] != ':') fail("expected :");
                i++;
                v.o->emplace_back(std::move(k), value());
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == '}') { i++; break; }
                fail("expected , or }");
            }
        } else if (c == '[') {
            v = Json::array(); i++; ws();
            if (i < s.size() && s[i] == ']') { i++; }
            else while (true) {
                v.a->push_back(value());
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == ']') { i++; break; }
                fail("expected , or ]");
            }
        } else if (c == '"') {
            v = Json::str(string());
        } else if (lit("true")) {
            v = Json::boolean(true);
        } else if (lit("false")) {
            v = Json::boolean(false);
        } else if (lit("null")) {
            v = Json();
        } else if (c == '-' || (c >= '0' && c <= '9')) {
            size_t st = i;
            bool fl = false;
            if (s[i] == '-') i++;
            while (i < s.size() && ((s[i] >= '0' && s[i] <= '9') || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+' || s[i] == '-')) {
                if (s[i] == '.' || s[i] == 'e' || s[i] == 'E') fl = true;
                i++;
            }
            std::string num = s.substr(st, i - st);
            if (!fl) {
                try { v = Json::integer(std::stoll(num)); } catch (...) { v = Json::number(std::stod(num)); }
            } else {
                v = Json::number(std::stod(num));
            }
        } else {
            fail("unexpected character");
        }
        depth--;
        return v;
    }
};

// Python json.dumps(ensure_ascii=True): ASCII as is (control characters escaped), every non-ASCII code point as \uXXXX, above U+FFFF as a
// surrogate pair. Invalid UTF-8 bytes (Python could not hold them) are escaped one by one as \u00XX, which round-trips stably.
// (Fixed 2026-11-19: the first version escaped each UTF-8 byte separately, found by fuzz_json; stored outputs were ASCII and unaffected.)
static void esc(std::string& out, uint32_t cp) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", unsigned(cp)); out += b; }

void dump_str(std::string& out, const std::string& s) {
    out += '"';
    const auto* p = reinterpret_cast<const unsigned char*>(s.data());
    size_t n = s.size(), i = 0;
    while (i < n) {
        unsigned char c = p[i];
        if (c < 0x80) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                case '\b': out += "\\b"; break;
                case '\f': out += "\\f"; break;
                default:
                    if (c < 0x20 || c == 0x7F) esc(out, c);
                    else out += char(c);
            }
            i++;
            continue;
        }
        int len = c >= 0xF0 && c <= 0xF4 ? 4 : c >= 0xE0 ? 3 : c >= 0xC2 && c <= 0xDF ? 2 : 0;
        uint32_t cp = len == 4 ? c & 0x07 : len == 3 ? c & 0x0F : c & 0x1F;
        bool ok = len > 0 && i + size_t(len) <= n;
        for (int k = 1; ok && k < len; k++) {
            if ((p[i + k] & 0xC0) != 0x80) ok = false;
            else cp = (cp << 6) | (p[i + k] & 0x3F);
        }
        if (ok && ((len == 3 && (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF))) || (len == 4 && (cp < 0x10000 || cp > 0x10FFFF)))) ok = false;
        if (!ok) { esc(out, c); i++; continue; }
        if (cp >= 0x10000) { cp -= 0x10000; esc(out, 0xD800 + (cp >> 10)); esc(out, 0xDC00 + (cp & 0x3FF)); }
        else esc(out, cp);
        i += size_t(len);
    }
    out += '"';
}

void dump(std::string& out, const Json& v, bool compact) {
    const char* item = compact ? "," : ", ";
    const char* kv = compact ? ":" : ": ";
    switch (v.t) {
        case Json::T::Null: out += "null"; break;
        case Json::T::Bool: out += v.b ? "true" : "false"; break;
        case Json::T::Int: out += std::to_string(v.i); break;
        case Json::T::Num: {
            if (std::isfinite(v.d) && v.d == std::floor(v.d) && std::fabs(v.d) < 1e16) { out += std::to_string(int64_t(v.d)); out += ".0"; }
            else { char b[32]; std::snprintf(b, sizeof b, "%.17g", v.d); out += b; }
            break;
        }
        case Json::T::Str: dump_str(out, v.s); break;
        case Json::T::Arr: {
            out += '[';
            for (size_t k = 0; k < v.a->size(); k++) { if (k) out += item; dump(out, (*v.a)[k], compact); }
            out += ']';
            break;
        }
        case Json::T::Obj: {
            out += '{';
            for (size_t k = 0; k < v.o->size(); k++) { if (k) out += item; dump_str(out, (*v.o)[k].first); out += kv; dump(out, (*v.o)[k].second, compact); }
            out += '}';
            break;
        }
    }
}
}  // namespace

Json json_parse(const std::string& text) {
    P p{text};
    Json v = p.value();
    p.ws();
    if (p.i != text.size()) p.fail("trailing data");
    return v;
}

std::string json_dump(const Json& v, bool compact) {
    std::string out;
    dump(out, v, compact);
    return out;
}

}  // namespace bsx
