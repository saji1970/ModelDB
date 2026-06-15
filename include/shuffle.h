#pragma once
#include <cstdint>
#include <vector>
#include <cstddef>

namespace memalgo {

// Byte Shuffle Filter for typed data arrays.
// Biological analogy: Chromosome sorting — during cell division,
// chromosomes are sorted by type before being packaged. Similarly,
// byte shuffle groups bytes by their positional role (exponent bytes
// together, mantissa bytes together) to reveal hidden structure
// that downstream compression stages can exploit.

class ByteShuffler {
public:
    // Detect optimal element size by analyzing data patterns.
    // Tries element sizes 2, 4, 8 and picks whichever produces
    // the lowest Shannon entropy after shuffling.
    // Returns 1 if no shuffle is beneficial.
    static uint8_t detect_element_size(const uint8_t* data, size_t len);

    // Shuffle: transpose byte planes.
    // Input:  [A0 A1 A2 A3 B0 B1 B2 B3 C0 C1 C2 C3 ...]  (element_size=4)
    // Output: [A0 B0 C0 ... A1 B1 C1 ... A2 B2 C2 ... A3 B3 C3 ...]
    static std::vector<uint8_t> shuffle(const uint8_t* data, size_t len, uint8_t element_size);
    static std::vector<uint8_t> shuffle(const std::vector<uint8_t>& data, uint8_t element_size);

    // Unshuffle: reverse the transpose (inverse of shuffle).
    static std::vector<uint8_t> unshuffle(const uint8_t* data, size_t len, uint8_t element_size);
    static std::vector<uint8_t> unshuffle(const std::vector<uint8_t>& data, uint8_t element_size);
};

} // namespace memalgo
