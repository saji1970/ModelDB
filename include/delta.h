#pragma once
#include "chunker.h"
#include <vector>
#include <cstdint>

namespace memalgo {

// Delta encoding for similar chunks.
// Like how neurons encode differences from a baseline (adaptation),
// similar chunks are stored as XOR deltas from a reference chunk
// with run-length encoding of the zero runs.

struct DeltaChunk {
    enum Type : uint8_t {
        FULL = 0,       // stored in full (reference chunk or no similar match)
        DELTA = 1,      // stored as delta from reference
    };

    Type type = FULL;
    uint32_t ref_id = 0;           // reference chunk id (for DELTA type)
    uint32_t original_size = 0;    // original chunk size (for size-mismatched deltas)
    std::vector<uint8_t> data;     // full data or delta-encoded data
};

class DeltaEncoder {
public:
    // Encode: find similar chunks and delta-encode them
    std::vector<DeltaChunk> encode(const std::vector<Chunk>& chunks);

    // Decode: restore original chunks from delta-encoded data
    std::vector<Chunk> decode(const std::vector<DeltaChunk>& delta_chunks);

private:
    // XOR two buffers and RLE-encode the result
    static std::vector<uint8_t> xor_rle_encode(const uint8_t* base, const uint8_t* target, size_t len);

    // Decode XOR+RLE delta back to target buffer
    static std::vector<uint8_t> xor_rle_decode(const uint8_t* base, size_t base_len,
                                                 const uint8_t* delta, size_t delta_len);

    // Compute a "similarity hash" from the first N bytes of a chunk
    static uint64_t similarity_hash(const uint8_t* data, size_t len);
};

} // namespace memalgo
