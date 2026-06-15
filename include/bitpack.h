#pragma once
#include <cstdint>
#include <vector>
#include <cstddef>

namespace memalgo {

// Bit-Packing Filter for low-cardinality data.
// Biological analogy: Genetic codon economy — DNA uses exactly
// 2 bits per nucleotide (4 states: A, C, G, T) rather than a
// full byte. Similarly, bit-packing uses only the minimum bits
// needed for the actual value range present in the data, which
// is critical for quantized ML model weights (INT4 = 4 bits,
// INT8 with limited range, etc.).

class BitPacker {
public:
    // Analyze data and determine minimum bits needed per value.
    // Returns bits_per_value (1-8). Returns 8 if no packing is beneficial.
    static uint8_t detect_bits_needed(const uint8_t* data, size_t len);

    // Pack: reduce each byte to minimum bits.
    // Format: bits_per_value(1) + original_len(4 LE) + num_distinct(1)
    //         + value_map(num_distinct bytes) + packed bitstream
    // If bits_per_value == 8, returns original data with 1-byte header (passthrough).
    static std::vector<uint8_t> pack(const uint8_t* data, size_t len);
    static std::vector<uint8_t> pack(const std::vector<uint8_t>& data);

    // Unpack: restore original byte values from packed data.
    static std::vector<uint8_t> unpack(const uint8_t* data, size_t len);
    static std::vector<uint8_t> unpack(const std::vector<uint8_t>& data);
};

} // namespace memalgo
