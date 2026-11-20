# C++ runtime of the on-chain swap tracker (docs/architecture.md). Build:
#   bash cpp/build.sh [release|asan]
cmake_minimum_required(VERSION 3.20)
project(bsx_chainscan CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(SQLite3 REQUIRED)
find_package(ZLIB REQUIRED)

option(BSX_ASAN "AddressSanitizer + UndefinedBehaviorSanitizer" OFF)

find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)
add_executable(bsx-swap-tracker src/main.cpp src/hash.cpp src/tx.cpp src/script.cpp src/json.cpp src/scan.cpp
  src/util.cpp src/settings.cpp src/config.cpp src/net.cpp src/source.cpp src/store.cpp src/scan_main.cpp src/watch.cpp src/match_main.cpp src/snapshot_main.cpp src/report_main.cpp)
target_link_libraries(bsx-swap-tracker PRIVATE SQLite::SQLite3 ZLIB::ZLIB OpenSSL::Crypto Threads::Threads)
target_compile_options(bsx-swap-tracker PRIVATE -Wall -Wextra -Werror -fstack-protector-strong)
target_compile_definitions(bsx-swap-tracker PRIVATE $<$<CONFIG:Release>:_FORTIFY_SOURCE=2>)
target_compile_definitions(bsx-swap-tracker PRIVATE BSX_TEST_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests")
if(BSX_ASAN)
  target_compile_options(bsx-swap-tracker PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer -g)
  target_link_options(bsx-swap-tracker PRIVATE -fsanitize=address,undefined)
endif()

# libFuzzer targets (clang only): cmake -DCMAKE_CXX_COMPILER=clang++ -DBSX_FUZZ=ON ; build via cpp/build.sh fuzz
option(BSX_FUZZ "libFuzzer targets for the parsers, scripts, JSON and P2P framing" OFF)
if(BSX_FUZZ)
  set(FUZZ_CORE src/hash.cpp src/tx.cpp src/script.cpp src/json.cpp src/scan.cpp src/util.cpp src/settings.cpp src/config.cpp src/net.cpp)
  foreach(t fuzz_block fuzz_tx fuzz_script fuzz_json fuzz_frame)
    add_executable(${t} fuzz/${t}.cpp ${FUZZ_CORE})
    target_compile_options(${t} PRIVATE -g -O1 -fsanitize=fuzzer,address,undefined -fno-omit-frame-pointer)
    target_link_options(${t} PRIVATE -fsanitize=fuzzer,address,undefined)
    target_link_libraries(${t} PRIVATE Threads::Threads)
  endforeach()
endif()
