#pragma once
#include <cstdint>
#include <vector>
#include <cstddef>

namespace memalgo {

// LZSS compression engine.
// Sliding-window compression with hash-chain match finding.
// Like how biological memory reuses existing "memories" by referencing
// earlier patterns instead of storing them again.

struct LZConfig {
    uint32_t window_bits = 16;   // 65536-byte window (was 12 / 4096)
    uint32_t min_match = 3;      // minimum match length
    uint32_t max_match = 258;    // maximum match length (was 18)
    uint32_t max_chain = 128;    // max hash chain traversal (was hardcoded 64)
    bool     lazy_match = true;  // enable lazy matching
};

class LZCompressor {
public:
    explicit LZCompressor(LZConfig cfg = {});

    // Compress data using LZSS
    std::vector<uint8_t> compress(const uint8_t* data, size_t len);
    std::vector<uint8_t> compress(const std::vector<uint8_t>& data);

    // Decompress LZSS data
    std::vector<uint8_t> decompress(const uint8_t* data, size_t len);
    std::vector<uint8_t> decompress(const std::vector<uint8_t>& data);

private:
    LZConfig cfg_;
    uint32_t window_size_;
    uint32_t window_mask_;

    // Hash chain for match finding — larger table for better distribution
    static constexpr int HASH_BITS = 15;
    static constexpr int HASH_SIZE = 1 << HASH_BITS; // 32768

    uint32_t hash3(const uint8_t* p) const {
        return ((uint32_t(p[0]) << 10) ^ (uint32_t(p[1]) << 5) ^ uint32_t(p[2])) & (HASH_SIZE - 1);
    }

    // Find best match at position pos
    struct Match { uint32_t offset; uint32_t length; };
    Match find_match(const uint8_t* data, size_t len, size_t pos,
                     const int32_t* hash_head, const int32_t* hash_chain) const;
};

} // namespace memalgo
