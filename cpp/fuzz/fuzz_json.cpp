// libFuzzer target: the JSON parser (RPC replies, board snapshots, status files). Property: parse or runtime_error; a parsed value
// re-serialises to text that parses back to the same serialisation (dump(parse(dump(v))) == dump(v)).
#include <cstdlib>
#include <stdexcept>
#include "../src/json.hpp"
using namespace bsx;
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    std::string s(reinterpret_cast<const char*>(data), size);
    try {
        Json v = json_parse(s);
        std::string a = json_dump(v), b = json_dump(v, true);
        if (json_dump(json_parse(a)) != a) abort();
        if (json_dump(json_parse(b), true) != b) abort();
    } catch (const std::runtime_error&) {
    } catch (const std::invalid_argument&) {
    } catch (const std::out_of_range&) {
    }
    return 0;
}
