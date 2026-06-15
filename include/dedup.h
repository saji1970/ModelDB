#pragma once
#include "utils.h"
#include "chunker.h"
#include <vector>
#include <unordered_map>
#include <cstdint>

namespace memalgo {

// Deduplication result: maps chunk indices to unique chunk IDs.
// Mimics content-addressable memory in biological systems —
// the same information pattern is stored once, referenced many times.
struct DedupResult {
    std::vector<Chunk> unique_chunks;               // only unique data
    std::vector<uint32_t> chunk_map;                // original_idx -> unique_chunk_id
    uint64_t total_original_size = 0;
    uint64_t total_unique_size = 0;
    uint32_t duplicate_count = 0;
};

class Deduplicator {
public:
    // Deduplicate a list of chunks. Returns unique chunks + mapping.
    DedupResult deduplicate(const std::vector<Chunk>& chunks);

    // Restore original chunk order from dedup result
    static std::vector<Chunk> restore(const DedupResult& result);

private:
    std::unordered_map<Hash128, uint32_t, Hash128Hasher> hash_map_;
};

} // namespace memalgo
