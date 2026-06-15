#include "dedup.h"

namespace memalgo {

DedupResult Deduplicator::deduplicate(const std::vector<Chunk>& chunks) {
    DedupResult result;
    hash_map_.clear();

    result.chunk_map.reserve(chunks.size());

    for (auto& chunk : chunks) {
        result.total_original_size += chunk.data.size();

        auto it = hash_map_.find(chunk.hash);
        if (it != hash_map_.end()) {
            // Duplicate found — just store the reference
            result.chunk_map.push_back(it->second);
            result.duplicate_count++;
        } else {
            // New unique chunk
            uint32_t uid = static_cast<uint32_t>(result.unique_chunks.size());
            hash_map_[chunk.hash] = uid;
            result.chunk_map.push_back(uid);

            Chunk uc;
            uc.data = chunk.data;
            uc.hash = chunk.hash;
            uc.id = uid;
            result.unique_chunks.push_back(std::move(uc));
            result.total_unique_size += chunk.data.size();
        }
    }

    return result;
}

std::vector<Chunk> Deduplicator::restore(const DedupResult& result) {
    std::vector<Chunk> out;
    out.reserve(result.chunk_map.size());

    for (size_t i = 0; i < result.chunk_map.size(); i++) {
        uint32_t uid = result.chunk_map[i];
        Chunk c;
        c.data = result.unique_chunks[uid].data;
        c.hash = result.unique_chunks[uid].hash;
        c.id = static_cast<uint32_t>(i);
        out.push_back(std::move(c));
    }

    return out;
}

} // namespace memalgo
