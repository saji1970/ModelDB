#include "chunker.h"
#include <cstring>

namespace memalgo {

uint32_t Chunker::buzhash_table_[256] = {};
bool Chunker::table_init_ = false;

// Initialize buzhash lookup table with deterministic pseudo-random values
void Chunker::init_table() {
    // Simple LCG seeded with a fixed value for reproducibility
    uint32_t state = 0x12345678u;
    for (int i = 0; i < 256; i++) {
        state = state * 1664525u + 1013904223u;
        buzhash_table_[i] = state;
    }
    table_init_ = true;
}

Chunker::Chunker(ChunkerConfig cfg) : cfg_(cfg) {
    if (!table_init_) init_table();

    // Compute mask from target chunk size
    // mask_ chosen so that (hash & mask_) == 0 triggers a boundary
    // at roughly 1/target_chunk frequency
    uint32_t bits = 0;
    size_t t = cfg_.target_chunk;
    while (t > 1) { t >>= 1; bits++; }
    mask_ = (1u << bits) - 1;
}

std::vector<Chunk> Chunker::chunk(const uint8_t* data, size_t len) {
    std::vector<Chunk> chunks;
    if (len == 0) return chunks;

    size_t pos = 0;
    uint32_t next_id = 0;

    while (pos < len) {
        size_t remaining = len - pos;
        size_t chunk_len;

        if (remaining <= cfg_.min_chunk) {
            // Remaining data too small — take it all
            chunk_len = remaining;
        } else {
            // Start rolling hash after min_chunk bytes
            uint32_t hash = 0;
            size_t window_size = 32; // rolling window size
            size_t i = cfg_.min_chunk;

            // Initialize hash with first window_size bytes at min_chunk offset
            size_t init_start = pos + cfg_.min_chunk;
            for (size_t w = 0; w < window_size && init_start + w < len; w++) {
                hash ^= buzhash_table_[data[init_start + w]]
                        << (uint32_t)((window_size - 1 - w) % 32);
            }

            chunk_len = cfg_.min_chunk;
            bool found_boundary = false;

            size_t limit = std::min(remaining, cfg_.max_chunk);
            for (i = cfg_.min_chunk; i < limit; i++) {
                // Check for boundary
                if ((hash & mask_) == 0) {
                    chunk_len = i;
                    found_boundary = true;
                    break;
                }

                // Roll the hash forward
                size_t out_idx = pos + i;
                size_t in_idx = pos + i + window_size;

                // Remove outgoing byte
                if (out_idx < len) {
                    hash ^= buzhash_table_[data[out_idx]]
                            << (uint32_t)((window_size - 1) % 32);
                }
                // Rotate left by 1
                hash = (hash << 1) | (hash >> 31);
                // Add incoming byte
                if (in_idx < len) {
                    hash ^= buzhash_table_[data[in_idx]];
                }
            }

            if (!found_boundary) {
                chunk_len = limit;
            }
        }

        Chunk c;
        c.data.assign(data + pos, data + pos + chunk_len);
        c.hash = fnv1a_128(data + pos, chunk_len);
        c.id = next_id++;
        chunks.push_back(std::move(c));

        pos += chunk_len;
    }

    return chunks;
}

std::vector<Chunk> Chunker::chunk(const std::vector<uint8_t>& data) {
    return chunk(data.data(), data.size());
}

std::vector<uint8_t> Chunker::reassemble(const std::vector<Chunk>& chunks) {
    size_t total = 0;
    for (auto& c : chunks) total += c.data.size();

    std::vector<uint8_t> out;
    out.reserve(total);
    for (auto& c : chunks) {
        out.insert(out.end(), c.data.begin(), c.data.end());
    }
    return out;
}

} // namespace memalgo
