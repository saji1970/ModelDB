#include "file_format.h"
#include <stdexcept>
#include <cstring>

namespace memalgo {

std::vector<uint8_t> CellFile::serialize() const {
    BufferWriter w;

    // --- Header (40 bytes) ---
    w.write_bytes(header.magic, 4);
    w.write_u16(header.version);
    w.write_u8(header.flags);
    w.write_u8(header.pipeline_config);
    w.write_u64(header.original_size);
    w.write_u64(header.compressed_payload_size);
    w.write_u32(header.chunk_count);
    w.write_u32(header.unique_chunk_count);
    w.write_u32(header.genome_size);
    w.write_u32(header.original_crc32);

    // --- Genome (if present) ---
    if (header.flags & FLAG_HAS_GENOME) {
        auto genome_data = genome.serialize();
        w.write_bytes(genome_data);
    }

    // --- Chunk references (chunk_count x uint32_t) ---
    // Maps original chunk order -> unique chunk id
    for (uint32_t i = 0; i < header.chunk_count; i++) {
        if (i < chunk_refs.size()) {
            w.write_u32(chunk_refs[i]);
        } else {
            w.write_u32(i);
        }
    }

    // --- Chunk map (unique_chunk_count entries) ---
    for (auto& entry : chunk_map) {
        w.write_u64(entry.hash.low);
        w.write_u64(entry.hash.high);
        w.write_u64(entry.compressed_offset);
        w.write_u32(entry.compressed_size);
        w.write_u32(entry.original_size);
    }

    // --- Compressed chunk data ---
    for (auto& chunk_data : compressed_chunks) {
        w.write_bytes(chunk_data);
    }

    // --- Payload CRC32 ---
    w.write_u32(payload_crc32);

    return w.take();
}

CellFile CellFile::deserialize(const std::vector<uint8_t>& data) {
    return deserialize(data.data(), data.size());
}

CellFile CellFile::deserialize(const uint8_t* data, size_t len) {
    CellFile cf;
    BufferReader r(data, len);

    // --- Header ---
    r.read_bytes_into(cf.header.magic, 4);
    if (std::memcmp(cf.header.magic, CELL_MAGIC, 4) != 0) {
        throw std::runtime_error("Invalid .cell file: bad magic bytes");
    }

    cf.header.version = r.read_u16();
    if (cf.header.version != CELL_VERSION && cf.header.version != CELL_VERSION_2) {
        throw std::runtime_error("Unsupported .cell file version");
    }

    cf.header.flags = r.read_u8();
    cf.header.pipeline_config = r.read_u8();
    cf.header.original_size = r.read_u64();
    cf.header.compressed_payload_size = r.read_u64();
    cf.header.chunk_count = r.read_u32();
    cf.header.unique_chunk_count = r.read_u32();
    cf.header.genome_size = r.read_u32();
    cf.header.original_crc32 = r.read_u32();

    // --- Genome ---
    if (cf.header.flags & FLAG_HAS_GENOME) {
        auto genome_bytes = r.read_bytes(cf.header.genome_size);
        cf.genome = Genome::deserialize(genome_bytes);
    }

    // --- Chunk references ---
    cf.chunk_refs.resize(cf.header.chunk_count);
    for (uint32_t i = 0; i < cf.header.chunk_count; i++) {
        cf.chunk_refs[i] = r.read_u32();
    }

    // --- Chunk map ---
    cf.chunk_map.resize(cf.header.unique_chunk_count);
    for (uint32_t i = 0; i < cf.header.unique_chunk_count; i++) {
        cf.chunk_map[i].hash.low = r.read_u64();
        cf.chunk_map[i].hash.high = r.read_u64();
        cf.chunk_map[i].compressed_offset = r.read_u64();
        cf.chunk_map[i].compressed_size = r.read_u32();
        cf.chunk_map[i].original_size = r.read_u32();
    }

    // --- Compressed chunk data ---
    cf.compressed_chunks.resize(cf.header.unique_chunk_count);
    for (uint32_t i = 0; i < cf.header.unique_chunk_count; i++) {
        cf.compressed_chunks[i] = r.read_bytes(cf.chunk_map[i].compressed_size);
    }

    // --- Payload CRC32 ---
    cf.payload_crc32 = r.read_u32();

    return cf;
}

} // namespace memalgo
