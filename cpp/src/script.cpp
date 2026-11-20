#include "script.hpp"

namespace bsx {

OutType out_type(View s) {
    size_t n = s.size();
    if (n == 25 && s[0] == 0x76 && s[1] == 0xA9 && s[2] == 0x14 && s[23] == 0x88 && s[24] == 0xAC) return {"p2pkh", s.subspan(3, 20)};
    if (n == 23 && s[0] == 0xA9 && s[1] == 0x14 && s[22] == 0x87) return {"p2sh", s.subspan(2, 20)};
    if (n == 22 && s[0] == 0x00 && s[1] == 0x14) return {"p2wpkh", s.subspan(2)};
    if (n == 34 && s[0] == 0x00 && s[1] == 0x20) return {"p2wsh", s.subspan(2)};
    if (n == 35 && s[0] == 0xAA && s[1] == 0x20 && s[34] == 0x87) return {"p2sh32", s.subspan(2, 32)};
    if (n == 34 && s[0] == 0x51 && s[1] == 0x20) return {"p2tr", s.subspan(2)};
    if (n >= 1 && s[0] == 0x6A) return {"opreturn", View()};
    return {"other", View()};
}

}  // namespace bsx
