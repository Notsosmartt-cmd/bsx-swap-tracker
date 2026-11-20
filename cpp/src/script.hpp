// Output script types (port of chainscan_lib.out_type). Templates and tiers are in scan.cpp.
#pragma once
#include <string>
#include "bytes.hpp"

namespace bsx {

struct OutType {
    const char* type;   // p2pkh p2sh p2wpkh p2wsh p2sh32 p2tr opreturn other
    View program;
};

OutType out_type(View s);

}  // namespace bsx
