// Minimal JSON: ordered objects (insertion order, as Python dicts), parser, and a writer that reproduces Python's json.dumps byte for byte for
// the values this program stores (ints, strings of ASCII, bools, null, nested lists/dicts): default separators ", " / ": ", or compact "," / ":".
#pragma once
#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace bsx {

struct Json;
using JsonArray = std::vector<Json>;
using JsonObject = std::vector<std::pair<std::string, Json>>;

struct Json {
    enum class T { Null, Bool, Int, Num, Str, Arr, Obj } t = T::Null;
    bool b = false;
    int64_t i = 0;
    double d = 0;
    std::string s;
    std::shared_ptr<JsonArray> a;
    std::shared_ptr<JsonObject> o;

    Json() = default;
    static Json null() { return Json(); }
    static Json boolean(bool v) { Json j; j.t = T::Bool; j.b = v; return j; }
    static Json integer(int64_t v) { Json j; j.t = T::Int; j.i = v; return j; }
    static Json number(double v) { Json j; j.t = T::Num; j.d = v; return j; }
    static Json str(std::string v) { Json j; j.t = T::Str; j.s = std::move(v); return j; }
    static Json array() { Json j; j.t = T::Arr; j.a = std::make_shared<JsonArray>(); return j; }
    static Json object() { Json j; j.t = T::Obj; j.o = std::make_shared<JsonObject>(); return j; }

    bool is_null() const { return t == T::Null; }
    Json& push(Json v) { a->push_back(std::move(v)); return a->back(); }
    Json& set(const std::string& k, Json v) {
        for (auto& kv : *o) if (kv.first == k) { kv.second = std::move(v); return kv.second; }
        o->emplace_back(k, std::move(v));
        return o->back().second;
    }
    const Json* get(const std::string& k) const {
        if (t != T::Obj) return nullptr;
        for (auto& kv : *o) if (kv.first == k) return &kv.second;
        return nullptr;
    }
    int64_t as_int(int64_t dflt = 0) const { return t == T::Int ? i : (t == T::Num ? int64_t(d) : dflt); }
    const std::string& as_str() const { static const std::string e; return t == T::Str ? s : e; }
};

Json json_parse(const std::string& text);                 // throws std::runtime_error
std::string json_dump(const Json& v, bool compact = false);  // Python json.dumps(v) or json.dumps(v, separators=(",", ":"))

// helpers for optional ints
inline Json jint_or_null(bool has, int64_t v) { return has ? Json::integer(v) : Json::null(); }

}  // namespace bsx
