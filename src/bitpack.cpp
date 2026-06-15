#include "bitpack.h"
#include <algorithm>
#include <cstring>

namespace memalgo {

uint8_t BitPacker::detect_bits_needed(const uint8_t* data, size_t len) {
    if (len == 0) return 8;

    // Count distinct byte values
    bool seen[256] = {};
    int distinct = 0;
    for (size_t i = 0; i < len; i++) {
        if (!seen[data[i]]) {
            seen[data[i]] = true;
            distinct++;
            if (distinct > 64) return 8; // too many distinct values, not worth packing
        }
    }

    uint8_t bits;
    if (distinct <= 2) bits = 1;
    else if (distinct <= 4) bits = 2;
    else if (distinct <= 16) bits = 4;
    else if (distinct <= 64) bits = 6;
    else return 8;

    // Check that overhead is worth it
    // Header: 1 (bits) + 4 (orig_len) + 1 (num_distinct) + distinct (value_map) = 6 + distinct
    size_t packed_data_bytes = (len * bits + 7) / 8;
    size_t total_packed = 6 + distinct + packed_data_bytes;
    if (total_packed >= len) return 8; // no benefit

    return bits;
}

std::vector<uint8_t> BitPacker::pack(const uint8_t* data, size_t len) {
    uint8_t bits = detect_bits_needed(data, len);

    if (bits == 8) {
        // Passthrough: just prepend header byte
        std::vector<uint8_t> out;
        out.reserve(1 + len);
        out.push_back(8); // bits_per_value = 8 means no packing
        out.insert(out.end(), data, data + len);
        return out;
    }

    // Build sorted value map: distinct values -> codes
    bool seen[256] = {};
    for (size_t i = 0; i < len; i++) seen[data[i]] = true;

    std::vector<uint8_t> value_map;
    uint8_t reverse_map[256] = {};
    for (int i = 0; i < 256; i++) {
        if (seen[i]) {
            reverse_map[i] = static_cast<uint8_t>(value_map.size());
            value_map.push_back(static_cast<uint8_t>(i));
        }
    }

    // Write header
    std::vector<uint8_t> out;
    size_t packed_bytes = (len * bits + 7) / 8;
    out.reserve(6 + value_map.size() + packed_bytes);

    out.push_back(bits); // bits_per_value
    // original length (4 bytes LE)
    uint32_t orig_len = static_cast<uint32_t>(len);
    out.push_back(static_cast<uint8_t>(orig_len & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_len >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_len >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_len >> 24) & 0xFF));
    // number of distinct values
    out.push_back(static_cast<uint8_t>(value_map.size()));
    // value map
    out.insert(out.end(), value_map.begin(), value_map.end());

    // Pack data into bitstream
    uint32_t bit_buffer = 0;
    int bits_in_buffer = 0;
    for (size_t i = 0; i < len; i++) {
        uint32_t code = reverse_map[data[i]];
        bit_buffer |= (code << bits_in_buffer);
        bits_in_buffer += bits;
        while (bits_in_buffer >= 8) {
            out.push_back(static_cast<uint8_t>(bit_buffer & 0xFF));
            bit_buffer >>= 8;
            bits_in_buffer -= 8;
        }
    }
    if (bits_in_buffer > 0) {
        out.push_back(static_cast<uint8_t>(bit_buffer & 0xFF));
    }

    return out;
}

std::vector<uint8_t> BitPacker::pack(const std::vector<uint8_t>& data) {
    return pack(data.data(), data.size());
}

std::vector<uint8_t> BitPacker::unpack(const uint8_t* data, size_t len) {
    if (len < 1) return {};

    uint8_t bits = data[0];

    if (bits == 8) {
        // Passthrough: strip header byte
        return std::vector<uint8_t>(data + 1, data + len);
    }

    if (len < 6) return {};

    // Read header
    uint32_t orig_len = static_cast<uint32_t>(data[1])
                      | (static_cast<uint32_t>(data[2]) << 8)
                      | (static_cast<uint32_t>(data[3]) << 16)
                      | (static_cast<uint32_t>(data[4]) << 24);
    uint8_t num_distinct = data[5];

    if (len < 6 + static_cast<size_t>(num_distinct)) return {};

    // Read value map
    const uint8_t* value_map = data + 6;
    size_t bitstream_start = 6 + num_distinct;

    // Unpack bitstream
    std::vector<uint8_t> out;
    out.reserve(orig_len);

    uint32_t mask = (1u << bits) - 1;
    uint32_t bit_buffer = 0;
    int bits_in_buffer = 0;
    size_t pos = bitstream_start;

    for (uint32_t i = 0; i < orig_len; i++) {
        while (bits_in_buffer < bits && pos < len) {
            bit_buffer |= (static_cast<uint32_t>(data[pos++]) << bits_in_buffer);
            bits_in_buffer += 8;
        }
        uint8_t code = static_cast<uint8_t>(bit_buffer & mask);
        bit_buffer >>= bits;
        bits_in_buffer -= bits;

        if (code < num_distinct) {
            out.push_back(value_map[code]);
        } else {
            out.push_back(0); // safety fallback
        }
    }

    return out;
}

std::vector<uint8_t> BitPacker::unpack(const std::vector<uint8_t>& data) {
    return unpack(data.data(), data.size());
}

} // namespace memalgo
