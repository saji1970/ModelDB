#pragma once
#include "utils.h"
#include <vector>
#include <cstdint>

namespace memalgo {

struct Chunk {
    std::vector<uint8_t> data;
    Hash128 hash;
    uint32_t id;
};

struct ChunkerConfig {
    size_t min_chunk  = 1024;       // 1 KB
    size_t target_chunk = 4096;     // 4 KB
    size_t max_chunk  = 16384;      // 16 KB
};

// Content-defined chunking using Buzhash rolling hash.
// Splits input data into variable-size chunks based on content boundaries.
// This mimics how biological systems segment information at natural boundaries
// rather than at fixed intervals.
class Chunker {
public:
    explicit Chunker(ChunkerConfig cfg = {});

    // Split data into chunks
    std::vector<Chunk> chunk(const uint8_t* data, size_t len);
    std::vector<Chunk> chunk(const std::vector<uint8_t>& data);

    // Reassemble chunks back into original data
    static std::vector<uint8_t> reassemble(const std::vector<Chunk>& chunks);

private:
    ChunkerConfig cfg_;
    uint32_t mask_;

    // Buzhash table (random values per byte)
    static uint32_t buzhash_table_[256];
    static bool table_init_;
    static void init_table();
};

} // namespace memalgo
