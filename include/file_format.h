#pragma once
#include "utils.h"
#include "cell.h"
#include "pattern_codec.h"
#include <vector>
#include <cstdint>
#include <string>

namespace memalgo {

// .cell file format constants
static constexpr uint8_t  CELL_MAGIC[4] = {0x43, 0x45, 0x4C, 0x4C}; // "CELL"
static constexpr uint16_t CELL_VERSION   = 0x0001;
static constexpr uint16_t CELL_VERSION_2 = 0x0002;

// Flags in file header
enum CellFlags : uint8_t {
    FLAG_HAS_GENOME  = 1 << 0,
    FLAG_HAS_DELTA   = 1 << 1,
    FLAG_HAS_DEDUP   = 1 << 2,
    FLAG_HAS_SHUFFLE = 1 << 3,
    FLAG_HAS_BITPACK = 1 << 4,
    FLAG_HAS_ORDER1  = 1 << 5,
};

// Per-chunk entry in the chunk map
struct ChunkMapEntry {
    Hash128  hash;
    uint64_t compressed_offset;
    uint32_t compressed_size;
    uint32_t original_size;
};

// Complete .cell file header
struct CellFileHeader {
    uint8_t  magic[4];
    uint16_t version;
    uint8_t  flags;
    uint8_t  pipeline_config;
    uint64_t original_size;
    uint64_t compressed_payload_size;
    uint32_t chunk_count;
    uint32_t unique_chunk_count;
    uint32_t genome_size;
    uint32_t original_crc32;
};

// The complete cell file data structure
struct CellFile {
    CellFileHeader header;
    Genome genome;
    std::vector<ChunkMapEntry> chunk_map;
    std::vector<uint32_t> chunk_refs;       // chunk_id -> unique_chunk_id mapping
    std::vector<std::vector<uint8_t>> compressed_chunks; // compressed data per unique chunk
    uint32_t payload_crc32;

    // Serialize to bytes
    std::vector<uint8_t> serialize() const;

    // Deserialize from bytes
    static CellFile deserialize(const std::vector<uint8_t>& data);
    static CellFile deserialize(const uint8_t* data, size_t len);
};

} // namespace memalgo
