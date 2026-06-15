#include "lz_compress.h"
#include <cstring>
#include <algorithm>

namespace memalgo {

LZCompressor::LZCompressor(LZConfig cfg) : cfg_(cfg) {
    window_size_ = 1u << cfg_.window_bits;
    window_mask_ = window_size_ - 1;
}

LZCompressor::Match LZCompressor::find_match(const uint8_t* data, size_t len, size_t pos,
                                               const int32_t* hash_head,
                                               const int32_t* hash_chain) const {
    Match best = {0, 0};
    if (pos + cfg_.min_match > len) return best;

    uint32_t h = hash3(data + pos);
    int32_t chain_idx = hash_head[h];
    int chain_left = static_cast<int>(cfg_.max_chain);

    while (chain_idx >= 0 && chain_left-- > 0) {
        size_t candidate = static_cast<size_t>(chain_idx);
        if (pos - candidate > window_size_) break;

        // Quick check: compare first and last bytes of current best before full scan
        if (best.length >= cfg_.min_match) {
            if (data[candidate + best.length] != data[pos + best.length] ||
                data[candidate] != data[pos]) {
                chain_idx = hash_chain[candidate & window_mask_];
                continue;
            }
        }

        // Full comparison
        size_t max_len = std::min(static_cast<size_t>(cfg_.max_match), len - pos);
        size_t match_len = 0;
        while (match_len < max_len && data[candidate + match_len] == data[pos + match_len]) {
            match_len++;
        }

        if (match_len >= cfg_.min_match && match_len > best.length) {
            best.offset = static_cast<uint32_t>(pos - candidate);
            best.length = static_cast<uint32_t>(match_len);
            if (best.length == cfg_.max_match) break; // can't do better
        }

        chain_idx = hash_chain[candidate & window_mask_];
    }

    return best;
}

// ============================================================
// LZSS Compress (v2 format)
// Format:
//   4 bytes: original size (little-endian)
//   1 byte:  format version (0x02 = v2 3-byte matches)
//   Then groups of:
//     1 flag byte (8 bits, LSB first):
//       bit=0: literal byte follows (1 byte)
//       bit=1: match follows (3 bytes: 16-bit offset, 8-bit length-3)
// ============================================================
std::vector<uint8_t> LZCompressor::compress(const uint8_t* data, size_t len) {
    std::vector<uint8_t> out;
    out.reserve(len);

    // Write original size
    uint32_t orig_size = static_cast<uint32_t>(len);
    out.push_back(static_cast<uint8_t>(orig_size & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_size >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_size >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig_size >> 24) & 0xFF));

    // Format version byte
    out.push_back(0x02); // v2 format

    if (len == 0) return out;

    // Hash chains
    std::vector<int32_t> hash_head(HASH_SIZE, -1);
    std::vector<int32_t> hash_chain(window_size_, -1);

    size_t pos = 0;
    while (pos < len) {
        // Flag byte position
        size_t flag_pos = out.size();
        out.push_back(0); // placeholder for flag byte
        uint8_t flags = 0;

        for (int bit = 0; bit < 8 && pos < len; bit++) {
            Match match = {0, 0};
            if (pos + cfg_.min_match <= len) {
                match = find_match(data, len, pos, hash_head.data(), hash_chain.data());
            }

            // Lazy matching: check if next position has a better match
            if (cfg_.lazy_match && match.length >= cfg_.min_match && pos + 1 + cfg_.min_match <= len) {
                // Insert current position into hash chain first
                uint32_t h = hash3(data + pos);
                hash_chain[pos & window_mask_] = hash_head[h];
                hash_head[h] = static_cast<int32_t>(pos);

                Match next_match = find_match(data, len, pos + 1, hash_head.data(), hash_chain.data());
                if (next_match.length > match.length + 1) {
                    // Next position is better — emit current as literal
                    out.push_back(data[pos]);
                    // flag bit stays 0 (literal)
                    pos++;
                    continue; // don't re-insert into hash
                }
            }

            if (match.length >= cfg_.min_match) {
                // Emit match: 3 bytes (v2 format)
                // Byte 0: offset low 8 bits
                // Byte 1: offset high 8 bits
                // Byte 2: length - min_match (0-255)
                flags |= (1 << bit);
                uint32_t offset = match.offset;
                uint32_t length_code = match.length - cfg_.min_match;
                out.push_back(static_cast<uint8_t>(offset & 0xFF));
                out.push_back(static_cast<uint8_t>((offset >> 8) & 0xFF));
                out.push_back(static_cast<uint8_t>(length_code & 0xFF));

                // Insert all matched positions into hash chain
                for (uint32_t j = 0; j < match.length && pos + j + cfg_.min_match <= len; j++) {
                    if (pos + j + 2 < len) {
                        uint32_t h = hash3(data + pos + j);
                        hash_chain[(pos + j) & window_mask_] = hash_head[h];
                        hash_head[h] = static_cast<int32_t>(pos + j);
                    }
                }
                pos += match.length;
            } else {
                // Emit literal
                out.push_back(data[pos]);

                // Insert into hash chain
                if (pos + 2 < len) {
                    uint32_t h = hash3(data + pos);
                    hash_chain[pos & window_mask_] = hash_head[h];
                    hash_head[h] = static_cast<int32_t>(pos);
                }
                pos++;
            }
        }

        out[flag_pos] = flags;
    }

    return out;
}

std::vector<uint8_t> LZCompressor::compress(const std::vector<uint8_t>& data) {
    return compress(data.data(), data.size());
}

// ============================================================
// LZSS Decompress (handles both v1 and v2 format)
// ============================================================
std::vector<uint8_t> LZCompressor::decompress(const uint8_t* data, size_t len) {
    if (len < 4) return {};

    // Read original size
    uint32_t orig_size = static_cast<uint32_t>(data[0])
                       | (static_cast<uint32_t>(data[1]) << 8)
                       | (static_cast<uint32_t>(data[2]) << 16)
                       | (static_cast<uint32_t>(data[3]) << 24);

    std::vector<uint8_t> out;
    out.reserve(orig_size);

    // Detect format version
    // v2 format has a version byte (0x02) at offset 4
    // v1 format starts directly with flag bytes (never 0x02 as first flag is unlikely to be exactly 0x02 but we use an explicit marker)
    size_t pos = 4;
    bool v2_format = false;
    if (pos < len && data[pos] == 0x02) {
        v2_format = true;
        pos++; // skip version byte
    }

    while (pos < len && out.size() < orig_size) {
        if (pos >= len) break;
        uint8_t flags = data[pos++];

        for (int bit = 0; bit < 8 && pos < len && out.size() < orig_size; bit++) {
            if (flags & (1 << bit)) {
                if (v2_format) {
                    // Match: 3 bytes (v2)
                    if (pos + 2 >= len) break;
                    uint8_t b0 = data[pos++];
                    uint8_t b1 = data[pos++];
                    uint8_t b2 = data[pos++];

                    uint32_t offset = static_cast<uint32_t>(b0) | (static_cast<uint32_t>(b1) << 8);
                    uint32_t length = static_cast<uint32_t>(b2) + cfg_.min_match;

                    if (offset == 0 || offset > out.size()) break;
                    size_t src = out.size() - offset;
                    for (uint32_t j = 0; j < length && out.size() < orig_size; j++) {
                        out.push_back(out[src + j]);
                    }
                } else {
                    // Match: 2 bytes (v1 legacy)
                    if (pos + 1 >= len) break;
                    uint8_t b0 = data[pos++];
                    uint8_t b1 = data[pos++];

                    uint32_t offset = static_cast<uint32_t>(b0) | ((static_cast<uint32_t>(b1) & 0x0F) << 8);
                    uint32_t length = ((b1 >> 4) & 0x0F) + cfg_.min_match;

                    if (offset == 0 || offset > out.size()) break;
                    size_t src = out.size() - offset;
                    for (uint32_t j = 0; j < length && out.size() < orig_size; j++) {
                        out.push_back(out[src + j]);
                    }
                }
            } else {
                // Literal
                out.push_back(data[pos++]);
            }
        }
    }

    return out;
}

std::vector<uint8_t> LZCompressor::decompress(const std::vector<uint8_t>& data) {
    return decompress(data.data(), data.size());
}

} // namespace memalgo
