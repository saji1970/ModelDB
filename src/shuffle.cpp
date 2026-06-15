#include "shuffle.h"
#include <cmath>
#include <algorithm>
#include <cstring>

namespace memalgo {

// Compute Shannon entropy of data in bits per byte
static double compute_entropy(const uint8_t* data, size_t len) {
    if (len == 0) return 0.0;
    uint32_t freq[256] = {};
    for (size_t i = 0; i < len; i++) freq[data[i]]++;

    double ent = 0.0;
    double inv = 1.0 / static_cast<double>(len);
    for (int i = 0; i < 256; i++) {
        if (freq[i] == 0) continue;
        double p = static_cast<double>(freq[i]) * inv;
        ent -= p * std::log2(p);
    }
    return ent;
}

uint8_t ByteShuffler::detect_element_size(const uint8_t* data, size_t len) {
    if (len < 8) return 1;

    // Sample first 16KB for speed
    size_t sample_len = std::min(len, size_t(16384));

    double base_entropy = compute_entropy(data, sample_len);

    uint8_t best_size = 1;
    double best_entropy = base_entropy;

    // Try element sizes 2, 4, 8
    uint8_t candidates[] = {2, 4, 8};
    for (uint8_t elem_size : candidates) {
        if (sample_len < elem_size * 2) continue;

        auto shuffled = shuffle(data, sample_len, elem_size);
        double ent = compute_entropy(shuffled.data(), shuffled.size());

        if (ent < best_entropy - 0.5) {
            best_entropy = ent;
            best_size = elem_size;
        }
    }

    return best_size;
}

std::vector<uint8_t> ByteShuffler::shuffle(const uint8_t* data, size_t len, uint8_t element_size) {
    if (element_size <= 1 || len < element_size) {
        return std::vector<uint8_t>(data, data + len);
    }

    size_t num_elements = len / element_size;
    size_t remainder = len % element_size;
    std::vector<uint8_t> out(len);

    // Transpose: group all byte-0 of each element, then byte-1, etc.
    for (uint8_t plane = 0; plane < element_size; plane++) {
        for (size_t e = 0; e < num_elements; e++) {
            out[plane * num_elements + e] = data[e * element_size + plane];
        }
    }

    // Copy remainder bytes unchanged
    size_t main_bytes = num_elements * element_size;
    for (size_t i = 0; i < remainder; i++) {
        out[main_bytes + i] = data[main_bytes + i];
    }

    return out;
}

std::vector<uint8_t> ByteShuffler::shuffle(const std::vector<uint8_t>& data, uint8_t element_size) {
    return shuffle(data.data(), data.size(), element_size);
}

std::vector<uint8_t> ByteShuffler::unshuffle(const uint8_t* data, size_t len, uint8_t element_size) {
    if (element_size <= 1 || len < element_size) {
        return std::vector<uint8_t>(data, data + len);
    }

    size_t num_elements = len / element_size;
    size_t remainder = len % element_size;
    std::vector<uint8_t> out(len);

    // Reverse transpose: read from planes back to interleaved
    for (uint8_t plane = 0; plane < element_size; plane++) {
        for (size_t e = 0; e < num_elements; e++) {
            out[e * element_size + plane] = data[plane * num_elements + e];
        }
    }

    // Copy remainder bytes unchanged
    size_t main_bytes = num_elements * element_size;
    for (size_t i = 0; i < remainder; i++) {
        out[main_bytes + i] = data[main_bytes + i];
    }

    return out;
}

std::vector<uint8_t> ByteShuffler::unshuffle(const std::vector<uint8_t>& data, uint8_t element_size) {
    return unshuffle(data.data(), data.size(), element_size);
}

} // namespace memalgo
