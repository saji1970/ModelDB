#include "delta.h"
#include "utils.h"
#include <unordered_map>
#include <algorithm>
#include <cstring>

namespace memalgo {

uint64_t DeltaEncoder::similarity_hash(const uint8_t* data, size_t len) {
    // Hash a broader sample of the data to detect similarity.
    // Sample beginning (128 bytes, every 4th = 32 samples) +
    // middle (64 bytes, every 4th = 16 samples) = 48 total samples.
    // (Was: only 16 samples from first 64 bytes)
    uint64_t h = 0xcbf29ce484222325ULL;

    // Sample from beginning
    size_t sample1 = std::min(len, size_t(128));
    for (size_t i = 0; i < sample1; i += 4) {
        h ^= static_cast<uint64_t>(data[i]);
        h *= 0x100000001b3ULL;
    }

    // Sample from middle
    if (len > 256) {
        size_t mid = len / 2;
        size_t sample2 = std::min(size_t(64), len - mid);
        for (size_t i = 0; i < sample2; i += 4) {
            h ^= static_cast<uint64_t>(data[mid + i]);
            h *= 0x100000001b3ULL;
        }
    }

    return h;
}

std::vector<uint8_t> DeltaEncoder::xor_rle_encode(const uint8_t* base, const uint8_t* target, size_t len) {
    // XOR the two buffers, then RLE-encode the result.
    std::vector<uint8_t> out;
    out.reserve(len);

    size_t i = 0;
    while (i < len) {
        uint8_t diff = base[i] ^ target[i];

        if (diff == 0) {
            // Count consecutive zeros
            size_t run = 0;
            while (i + run < len && run < 255 && (base[i + run] ^ target[i + run]) == 0) {
                run++;
            }
            out.push_back(static_cast<uint8_t>(run));
            out.push_back(0x00);
            i += run;
        } else {
            // Count consecutive non-zeros (up to 255)
            size_t start = i;
            size_t run = 0;
            while (i + run < len && run < 255 && (base[i + run] ^ target[i + run]) != 0) {
                run++;
            }
            out.push_back(static_cast<uint8_t>(run));
            out.push_back(0x01);
            for (size_t j = 0; j < run; j++) {
                out.push_back(base[start + j] ^ target[start + j]);
            }
            i += run;
        }
    }

    return out;
}

std::vector<uint8_t> DeltaEncoder::xor_rle_decode(const uint8_t* base, size_t base_len,
                                                    const uint8_t* delta, size_t delta_len) {
    std::vector<uint8_t> out(base_len);
    std::memcpy(out.data(), base, base_len);

    size_t di = 0;
    size_t oi = 0;

    while (di < delta_len && oi < base_len) {
        uint8_t count = delta[di++];
        if (di >= delta_len) break;
        uint8_t marker = delta[di++];

        if (marker == 0x00) {
            // Zero run — no changes, skip 'count' bytes
            oi += count;
        } else {
            // Non-zero diffs follow
            for (uint8_t j = 0; j < count && di < delta_len && oi < base_len; j++) {
                out[oi] ^= delta[di++];
                oi++;
            }
        }
    }

    return out;
}

std::vector<DeltaChunk> DeltaEncoder::encode(const std::vector<Chunk>& chunks) {
    std::vector<DeltaChunk> result;
    result.reserve(chunks.size());

    // Build similarity buckets: similarity_hash -> list of chunk indices
    // (was: single chunk index — now keeps multiple candidates for best delta selection)
    std::unordered_map<uint64_t, std::vector<uint32_t>> sim_buckets;

    for (size_t i = 0; i < chunks.size(); i++) {
        const auto& chunk = chunks[i];
        DeltaChunk dc;

        if (chunk.data.size() < 64) {
            // Too small for delta encoding — store full
            dc.type = DeltaChunk::FULL;
            dc.original_size = static_cast<uint32_t>(chunk.data.size());
            dc.data = chunk.data;
            result.push_back(std::move(dc));
            continue;
        }

        uint64_t sh = similarity_hash(chunk.data.data(), chunk.data.size());
        auto it = sim_buckets.find(sh);

        bool delta_found = false;
        if (it != sim_buckets.end()) {
            // Try all candidates, pick the best delta
            std::vector<uint8_t> best_delta;
            uint32_t best_ref = 0;

            for (uint32_t ref : it->second) {
                const auto& ref_chunk = chunks[ref];

                // Allow delta for chunks within 10% size difference (was: exact same size only)
                size_t max_size = std::max(ref_chunk.data.size(), chunk.data.size());
                size_t min_size = std::min(ref_chunk.data.size(), chunk.data.size());
                if (min_size * 10 < max_size * 9) continue; // more than ~10% size diff

                // Pad shorter to match longer for XOR
                std::vector<uint8_t> ref_padded = ref_chunk.data;
                std::vector<uint8_t> cur_padded = chunk.data;
                ref_padded.resize(max_size, 0);
                cur_padded.resize(max_size, 0);

                auto delta_data = xor_rle_encode(ref_padded.data(),
                                                   cur_padded.data(),
                                                   max_size);

                // Only use delta if it's at least 20% smaller (was 10%)
                if (delta_data.size() < chunk.data.size() * 4 / 5) {
                    if (best_delta.empty() || delta_data.size() < best_delta.size()) {
                        best_delta = std::move(delta_data);
                        best_ref = ref;
                    }
                }
            }

            if (!best_delta.empty()) {
                dc.type = DeltaChunk::DELTA;
                dc.ref_id = best_ref;
                dc.original_size = static_cast<uint32_t>(chunk.data.size());
                dc.data = std::move(best_delta);
                result.push_back(std::move(dc));
                delta_found = true;
            }
        }

        if (!delta_found) {
            // Store full and register as reference candidate
            sim_buckets[sh].push_back(static_cast<uint32_t>(i));
            dc.type = DeltaChunk::FULL;
            dc.original_size = static_cast<uint32_t>(chunk.data.size());
            dc.data = chunk.data;
            result.push_back(std::move(dc));
        }
    }

    return result;
}

std::vector<Chunk> DeltaEncoder::decode(const std::vector<DeltaChunk>& delta_chunks) {
    std::vector<Chunk> out;
    out.reserve(delta_chunks.size());

    for (size_t i = 0; i < delta_chunks.size(); i++) {
        const auto& dc = delta_chunks[i];
        Chunk c;
        c.id = static_cast<uint32_t>(i);

        if (dc.type == DeltaChunk::FULL) {
            c.data = dc.data;
        } else {
            // Delta: reconstruct from reference
            if (dc.ref_id >= out.size()) {
                c.data = dc.data;
            } else {
                const auto& ref = out[dc.ref_id];
                c.data = xor_rle_decode(ref.data.data(), ref.data.size(),
                                         dc.data.data(), dc.data.size());
                // Trim to original size (for size-mismatched deltas)
                if (dc.original_size > 0 && c.data.size() > dc.original_size) {
                    c.data.resize(dc.original_size);
                }
            }
        }

        c.hash = fnv1a_128(c.data.data(), c.data.size());
        out.push_back(std::move(c));
    }

    return out;
}

} // namespace memalgo
