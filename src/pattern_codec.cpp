#include "pattern_codec.h"
#include <algorithm>
#include <unordered_map>
#include <cstring>

namespace memalgo {

// ============================================================
// Genome serialization
// ============================================================
std::vector<uint8_t> Genome::serialize() const {
    // Format: escape_byte(1) + entry_count(2) + entries[](pattern_len(1) + pattern(2-8) + code(1))
    std::vector<uint8_t> out;
    out.push_back(escape_byte);
    out.push_back(static_cast<uint8_t>(entry_count & 0xFF));
    out.push_back(static_cast<uint8_t>((entry_count >> 8) & 0xFF));

    for (uint16_t i = 0; i < entry_count; i++) {
        out.push_back(entries[i].pattern_len);
        for (uint8_t j = 0; j < entries[i].pattern_len; j++) {
            out.push_back(entries[i].pattern[j]);
        }
        out.push_back(entries[i].code);
    }
    return out;
}

Genome Genome::deserialize(const uint8_t* data, size_t len) {
    Genome g;
    if (len < 3) return g;

    size_t pos = 0;
    g.escape_byte = data[pos++];
    g.entry_count = static_cast<uint16_t>(data[pos]) | (static_cast<uint16_t>(data[pos + 1]) << 8);
    pos += 2;

    for (uint16_t i = 0; i < g.entry_count && pos < len; i++) {
        g.entries[i].pattern_len = data[pos++];
        if (g.entries[i].pattern_len > 8) g.entries[i].pattern_len = 8; // safety
        for (uint8_t j = 0; j < g.entries[i].pattern_len && pos < len; j++) {
            g.entries[i].pattern[j] = data[pos++];
        }
        if (pos < len) {
            g.entries[i].code = data[pos++];
        }
    }
    return g;
}

Genome Genome::deserialize(const std::vector<uint8_t>& data) {
    return deserialize(data.data(), data.size());
}

// ============================================================
// Pattern frequency counting
// ============================================================

struct PatternFreq {
    uint8_t pattern[8];
    uint8_t len;
    uint64_t freq;
    double savings;
};

Genome PatternCodec::build_genome(const uint8_t* data, size_t len) {
    Genome genome;
    if (len < 4) {
        genome.entry_count = 0;
        return genome;
    }

    // Step 1: Find least frequent byte for escape
    uint32_t freq[256] = {};
    for (size_t i = 0; i < len; i++) freq[data[i]]++;

    uint8_t escape = 0;
    uint32_t min_freq = freq[0];
    for (int i = 1; i < 256; i++) {
        if (freq[i] < min_freq) {
            min_freq = freq[i];
            escape = static_cast<uint8_t>(i);
        }
    }
    genome.escape_byte = escape;

    // Step 2: Count 2-byte through 8-byte pattern frequencies
    struct PatternKey {
        uint8_t bytes[8];
        uint8_t len;
        bool operator==(const PatternKey& o) const {
            return len == o.len && std::memcmp(bytes, o.bytes, len) == 0;
        }
    };
    struct PatternKeyHash {
        size_t operator()(const PatternKey& k) const {
            size_t h = 0xcbf29ce484222325ULL;
            for (uint8_t i = 0; i < k.len; i++) {
                h ^= k.bytes[i];
                h *= 0x100000001b3ULL;
            }
            h ^= k.len;
            return h;
        }
    };

    std::unordered_map<PatternKey, uint64_t, PatternKeyHash> pattern_freq;

    // Count patterns (sample if data is very large)
    size_t sample_len = std::min(len, size_t(256 * 1024));
    for (size_t plen = 2; plen <= 8; plen++) {
        if (sample_len < plen) continue;
        for (size_t i = 0; i <= sample_len - plen; i++) {
            PatternKey key;
            key.len = static_cast<uint8_t>(plen);
            std::memcpy(key.bytes, data + i, plen);
            pattern_freq[key]++;
        }
    }

    // Step 3: Score patterns by savings
    std::vector<PatternFreq> candidates;
    for (auto& [key, count] : pattern_freq) {
        if (count < 4) continue;

        PatternFreq pf;
        std::memcpy(pf.pattern, key.bytes, key.len);
        pf.len = key.len;
        pf.freq = count;
        pf.savings = static_cast<double>(count) * (static_cast<double>(key.len) - 2.0);
        candidates.push_back(pf);
    }

    // Sort by savings descending
    std::sort(candidates.begin(), candidates.end(),
              [](const PatternFreq& a, const PatternFreq& b) {
                  return a.savings > b.savings;
              });

    // Step 4: Select top-N patterns (max 255)
    uint16_t count_limit = std::min(static_cast<size_t>(255), candidates.size());

    genome.entry_count = 0;
    for (uint16_t i = 0; i < count_limit; i++) {
        if (candidates[i].savings <= 0 && candidates[i].len <= 2) continue;
        if (candidates[i].freq < 4) continue;

        auto& e = genome.entries[genome.entry_count];
        std::memcpy(e.pattern, candidates[i].pattern, candidates[i].len);
        e.pattern_len = candidates[i].len;
        e.code = static_cast<uint8_t>(genome.entry_count);
        genome.entry_count++;

        if (genome.entry_count >= 255) break;
    }

    return genome;
}

Genome PatternCodec::build_genome(const std::vector<uint8_t>& data) {
    return build_genome(data.data(), data.size());
}

// ============================================================
// Encode: Replace patterns with escape+code pairs
// Uses hash map for O(1) pattern lookup instead of linear scan
// ============================================================
std::vector<uint8_t> PatternCodec::encode(const uint8_t* data, size_t len, const Genome& genome) {
    std::vector<uint8_t> out;
    out.reserve(len);

    if (genome.entry_count == 0) {
        // No patterns — just escape existing escape bytes
        for (size_t i = 0; i < len; i++) {
            if (data[i] == genome.escape_byte) {
                out.push_back(genome.escape_byte);
                out.push_back(0xFF);
            } else {
                out.push_back(data[i]);
            }
        }
        return out;
    }

    // Build hash-map lookup tables grouped by pattern length
    // Key: FNV hash of pattern bytes, Value: entry index
    struct LookupEntry {
        uint16_t entry_idx;
    };
    std::unordered_map<uint64_t, LookupEntry> lookup[9]; // index by pattern length 0-8

    for (uint16_t e = 0; e < genome.entry_count; e++) {
        uint64_t h = 0xcbf29ce484222325ULL;
        for (uint8_t j = 0; j < genome.entries[e].pattern_len; j++) {
            h ^= genome.entries[e].pattern[j];
            h *= 0x100000001b3ULL;
        }
        lookup[genome.entries[e].pattern_len][h] = {e};
    }

    // Find max pattern length in genome for search boundary
    uint8_t max_plen = 2;
    for (uint16_t e = 0; e < genome.entry_count; e++) {
        if (genome.entries[e].pattern_len > max_plen)
            max_plen = genome.entries[e].pattern_len;
    }

    size_t i = 0;
    while (i < len) {
        bool matched = false;

        // Try matching patterns (longest first)
        for (int plen = max_plen; plen >= 2 && !matched; plen--) {
            if (i + plen > len) continue;
            if (lookup[plen].empty()) continue;

            // Hash the next plen bytes
            uint64_t h = 0xcbf29ce484222325ULL;
            for (int j = 0; j < plen; j++) {
                h ^= data[i + j];
                h *= 0x100000001b3ULL;
            }

            auto it = lookup[plen].find(h);
            if (it != lookup[plen].end()) {
                uint16_t eidx = it->second.entry_idx;
                // Verify exact match (hash collision guard)
                if (std::memcmp(data + i, genome.entries[eidx].pattern, plen) == 0) {
                    out.push_back(genome.escape_byte);
                    out.push_back(genome.entries[eidx].code);
                    i += plen;
                    matched = true;
                }
            }
        }

        if (!matched) {
            if (data[i] == genome.escape_byte) {
                out.push_back(genome.escape_byte);
                out.push_back(0xFF);
            } else {
                out.push_back(data[i]);
            }
            i++;
        }
    }

    return out;
}

std::vector<uint8_t> PatternCodec::encode(const std::vector<uint8_t>& data, const Genome& genome) {
    return encode(data.data(), data.size(), genome);
}

// ============================================================
// Decode: Restore patterns from escape+code pairs
// Uses direct lookup table for O(1) code->entry resolution
// ============================================================
std::vector<uint8_t> PatternCodec::decode(const uint8_t* data, size_t len, const Genome& genome) {
    std::vector<uint8_t> out;
    out.reserve(len);

    // Build direct lookup: code -> entry index (O(1) decode)
    uint16_t code_to_entry[256];
    std::memset(code_to_entry, 0xFF, sizeof(code_to_entry)); // 0xFFFF = invalid
    for (uint16_t e = 0; e < genome.entry_count; e++) {
        code_to_entry[genome.entries[e].code] = e;
    }

    size_t i = 0;
    while (i < len) {
        if (data[i] == genome.escape_byte) {
            i++;
            if (i >= len) break;

            if (data[i] == 0xFF) {
                // Literal escape byte
                out.push_back(genome.escape_byte);
            } else {
                // Pattern code — direct lookup
                uint8_t code = data[i];
                uint16_t eidx = code_to_entry[code];
                if (eidx != 0xFFFF) {
                    const auto& entry = genome.entries[eidx];
                    for (uint8_t j = 0; j < entry.pattern_len; j++) {
                        out.push_back(entry.pattern[j]);
                    }
                } else {
                    // Unknown code — output raw (shouldn't happen with valid data)
                    out.push_back(genome.escape_byte);
                    out.push_back(code);
                }
            }
            i++;
        } else {
            out.push_back(data[i]);
            i++;
        }
    }

    return out;
}

std::vector<uint8_t> PatternCodec::decode(const std::vector<uint8_t>& data, const Genome& genome) {
    return decode(data.data(), data.size(), genome);
}

} // namespace memalgo
