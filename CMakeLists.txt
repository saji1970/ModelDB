cmake_minimum_required(VERSION 3.14)
project(memalgo VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Source files
set(MEMALGO_SOURCES
    src/utils.cpp
    src/cell.cpp
    src/analyzer.cpp
    src/chunker.cpp
    src/dedup.cpp
    src/pattern_codec.cpp
    src/delta.cpp
    src/lz_compress.cpp
    src/entropy.cpp
    src/shuffle.cpp
    src/bitpack.cpp
    src/file_format.cpp
    src/memalgo.cpp
    src/memalgo_api.cpp
)

# Library (for tests and reuse)
add_library(memalgo_lib STATIC ${MEMALGO_SOURCES})
target_include_directories(memalgo_lib PUBLIC include)

if(MSVC)
    target_compile_options(memalgo_lib PRIVATE /W4 /O2)
else()
    target_compile_options(memalgo_lib PRIVATE -Wall -Wextra -O2)
endif()

# Main executable
add_executable(memalgo src/main.cpp)
target_link_libraries(memalgo PRIVATE memalgo_lib)

# Test executable
add_executable(memalgo_test tests/test_roundtrip.cpp)
target_link_libraries(memalgo_test PRIVATE memalgo_lib)

# Install
install(TARGETS memalgo RUNTIME DESTINATION bin)
