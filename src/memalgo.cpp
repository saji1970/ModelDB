#include "memalgo.h"
#include "shuffle.h"
#include "bitpack.h"
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <sstream>
#include <thread>
#include <vector>

namespace memalgo {

void MemAlgoEngine::log(const char* stage, const char* msg) {
    if (verbose_) verbose_(stage, msg);
}

AnalysisResult MemAlgoEngine::analyze(const std::vector<uint8_t>& data) {
    return memalgo::analyze(data);
}

// ============================================================
// Compress a single chunk through the sub-pipeline
// Order: bitpack -> shuffle -> pattern_encode -> LZ -> entropy
// Then prepend metadata: [shuffle_elem_size(1)] [bitpack_flag(1)] [pattern_flag(1)]
// ============================================================
std::vector<uint8_t> MemAlgoEngine::compress_chunk(const std::vector<uint8_t>& chunk_data,
                                                     uint8_t stages, const Genome& genome) {
    std::vector<uint8_t> current = chunk_data;

    // Stage: Bit-packing (for low-cardinality data like INT4/INT8 quantized weights)
    uint8_t bitpack_flag = 0; // 0 = not packed
    if (stages & STAGE_BITPACK) {
        auto packed = BitPacker::pack(current.data(), current.size());
        if (packed.size() < current.size()) {
            current = std::move(packed);
            bitpack_flag = 1;
        }
    }

    // Stage: Byte shuffle (for typed numerical arrays like float32/float16)
    uint8_t shuffle_elem_size = 1; // 1 = no shuffle
    if (stages & STAGE_SHUFFLE) {
        shuffle_elem_size = ByteShuffler::detect_element_size(current.data(), current.size());
        if (shuffle_elem_size > 1) {
            current = ByteShuffler::shuffle(current, shuffle_elem_size);
        }
    }

    // Stage: Pattern codec
    uint8_t pattern_flag = 0; // 0 = pattern encoding not applied
    if ((stages & STAGE_PATTERN) && genome.entry_count > 0) {
        PatternCodec codec;
        auto encoded = codec.encode(current, genome);
        // Only use if it actually reduced size
        if (encoded.size() < current.size()) {
            current = std::move(encoded);
            pattern_flag = 1;
        }
    }

    // Stage: LZ compression
    if (stages & STAGE_LZ) {
        LZCompressor lz;
        auto compressed = lz.compress(current);
        // LZ always includes a 5-byte header (4 size + 1 version), so compare fairly
        if (compressed.size() < current.size() + 5) {
            current = std::move(compressed);
        } else {
            // LZ made it bigger — prepend a marker to indicate "no LZ"
            // Use a special 4-byte "no LZ" header: size = 0
            std::vector<uint8_t> passthrough;
            passthrough.push_back(0);
            passthrough.push_back(0);
            passthrough.push_back(0);
            passthrough.push_back(0);
            passthrough.insert(passthrough.end(), current.begin(), current.end());
            current = std::move(passthrough);
        }
    }

    // Stage: Entropy coding (order-1 context-adaptive rANS with auto fallback to order-0)
    if (stages & STAGE_ENTROPY) {
        RANSEncoderOrder1 rans;
        auto encoded = rans.encode(current);
        // Only use if it reduced size
        if (encoded.size() < current.size()) {
            // Prepend 1-byte flag: 1 = rANS used
            std::vector<uint8_t> result;
            result.push_back(1);
            result.insert(result.end(), encoded.begin(), encoded.end());
            current = std::move(result);
        } else {
            // Prepend 1-byte flag: 0 = rANS skipped
            std::vector<uint8_t> result;
            result.push_back(0);
            result.insert(result.end(), current.begin(), current.end());
            current = std::move(result);
        }
    }

    // Prepend metadata bytes for new stages (read first during decompress)
    std::vector<uint8_t> final_output;
    final_output.reserve(3 + current.size());
    final_output.push_back(shuffle_elem_size);
    final_output.push_back(bitpack_flag);
    final_output.push_back(pattern_flag);
    final_output.insert(final_output.end(), current.begin(), current.end());

    return final_output;
}

// ============================================================
// Decompress a single chunk (reverse pipeline)
// Order: read metadata -> entropy -> LZ -> pattern_decode -> unshuffle -> unbitpack
// ============================================================
std::vector<uint8_t> MemAlgoEngine::decompress_chunk(const std::vector<uint8_t>& compressed,
                                                       uint8_t stages, const Genome& genome) {
    std::vector<uint8_t> current = compressed;

    // Read metadata bytes prepended during compression
    if (current.size() < 3) return current;
    uint8_t shuffle_elem_size = current[0];
    uint8_t bitpack_flag = current[1];
    uint8_t pattern_flag = current[2];
    current.erase(current.begin(), current.begin() + 3);

    // Stage: Entropy decoding (order-1 context-adaptive rANS)
    if (stages & STAGE_ENTROPY) {
        if (current.empty()) return current;
        uint8_t rans_flag = current[0];
        current.erase(current.begin());

        if (rans_flag == 1) {
            RANSEncoderOrder1 rans;
            current = rans.decode(current);
        }
    }

    // Stage: LZ decompression
    if (stages & STAGE_LZ) {
        if (current.size() >= 4) {
            // Check for "no LZ" marker (size = 0)
            uint32_t orig_size = static_cast<uint32_t>(current[0])
                               | (static_cast<uint32_t>(current[1]) << 8)
                               | (static_cast<uint32_t>(current[2]) << 16)
                               | (static_cast<uint32_t>(current[3]) << 24);
            if (orig_size == 0) {
                // No LZ was applied — strip header
                current.erase(current.begin(), current.begin() + 4);
            } else {
                LZCompressor lz;
                current = lz.decompress(current);
            }
        }
    }

    // Stage: Pattern decode (only if pattern encoding was actually applied)
    if ((stages & STAGE_PATTERN) && genome.entry_count > 0 && pattern_flag == 1) {
        PatternCodec codec;
        current = codec.decode(current, genome);
    }

    // Stage: Byte unshuffle (reverse of shuffle)
    if ((stages & STAGE_SHUFFLE) && shuffle_elem_size > 1) {
        current = ByteShuffler::unshuffle(current, shuffle_elem_size);
    }

    // Stage: Bit-unpack (reverse of bitpack)
    if ((stages & STAGE_BITPACK) && bitpack_flag == 1) {
        current = BitPacker::unpack(current.data(), current.size());
    }

    return current;
}

// ============================================================
// Full compression pipeline
// ============================================================
std::vector<uint8_t> MemAlgoEngine::compress(const std::vector<uint8_t>& input) {
    return compress(input.data(), input.size());
}

std::vector<uint8_t> MemAlgoEngine::compress(const uint8_t* data, size_t len) {
    stats_ = CompressionStats{};
    stats_.original_size = len;

    // Stage 1: Analyze
    log("analyze", "Classifying data...");
    auto analysis = memalgo::analyze(data, len);
    stats_.entropy = analysis.entropy;
    stats_.data_type = analysis.type_name();
    uint8_t stages = analysis.recommended_stages;
    stats_.stages_used = stages;

    char msg_buf[256];
    std::snprintf(msg_buf, sizeof(msg_buf),
                  "Type: %s, entropy: %.2f bits/byte, stages: 0x%02X",
                  analysis.type_name(), analysis.entropy, stages);
    log("analyze", msg_buf);

    // Handle empty input
    if (len == 0) {
        CellFile cf;
        std::memcpy(cf.header.magic, CELL_MAGIC, 4);
        cf.header.version = CELL_VERSION_2;
        cf.header.flags = 0;
        cf.header.pipeline_config = stages;
        cf.header.original_size = 0;
        cf.header.compressed_payload_size = 0;
        cf.header.chunk_count = 0;
        cf.header.unique_chunk_count = 0;
        cf.header.genome_size = 0;
        cf.header.original_crc32 = 0;
        cf.payload_crc32 = 0;
        stats_.compressed_size = 40; // header only
        return cf.serialize();
    }

    // Stage 2: Chunk
    log("chunk", "Content-defined chunking...");
    Chunker chunker;
    auto chunks = chunker.chunk(data, len);
    stats_.chunk_count = static_cast<uint32_t>(chunks.size());
    std::snprintf(msg_buf, sizeof(msg_buf), "%u chunks created", stats_.chunk_count);
    log("chunk", msg_buf);

    // Stage 3: Deduplicate
    log("dedup", "Deduplicating chunks...");
    Deduplicator dedup;
    auto dedup_result = dedup.deduplicate(chunks);
    stats_.unique_chunks = static_cast<uint32_t>(dedup_result.unique_chunks.size());
    stats_.duplicate_chunks = dedup_result.duplicate_count;
    stats_.size_after_dedup = dedup_result.total_unique_size;
    std::snprintf(msg_buf, sizeof(msg_buf), "%u unique, %u duplicates eliminated",
                  stats_.unique_chunks, stats_.duplicate_chunks);
    log("dedup", msg_buf);

    // Stage 4: Build pattern genome from all unique chunk data
    Genome genome;
    genome.entry_count = 0;
    if (stages & STAGE_PATTERN) {
        log("genome", "Building pattern genome...");
        // Concatenate all unique chunks for pattern analysis
        std::vector<uint8_t> all_data;
        for (auto& c : dedup_result.unique_chunks) {
            all_data.insert(all_data.end(), c.data.begin(), c.data.end());
        }
        PatternCodec codec;
        genome = codec.build_genome(all_data);
        std::snprintf(msg_buf, sizeof(msg_buf), "%u patterns in genome, escape=0x%02X",
                      genome.entry_count, genome.escape_byte);
        log("genome", msg_buf);
    }

    // Stage 5-7: Compress each unique chunk through sub-pipeline
    log("compress", "Compressing chunks through pipeline...");
    std::vector<std::vector<uint8_t>> compressed_chunks;
    compressed_chunks.reserve(dedup_result.unique_chunks.size());

    uint64_t total_compressed = 0;
    for (size_t i = 0; i < dedup_result.unique_chunks.size(); i++) {
        auto compressed = compress_chunk(dedup_result.unique_chunks[i].data, stages, genome);
        total_compressed += compressed.size();
        compressed_chunks.push_back(std::move(compressed));
    }
    stats_.size_after_lz = total_compressed;

    // Build the CellFile
    CellFile cf;
    std::memcpy(cf.header.magic, CELL_MAGIC, 4);
    cf.header.version = CELL_VERSION_2;
    cf.header.flags = 0;
    if (genome.entry_count > 0) cf.header.flags |= FLAG_HAS_GENOME;
    if (stages & STAGE_DEDUP) cf.header.flags |= FLAG_HAS_DEDUP;
    if (stages & STAGE_DELTA) cf.header.flags |= FLAG_HAS_DELTA;
    if (stages & STAGE_SHUFFLE) cf.header.flags |= FLAG_HAS_SHUFFLE;
    if (stages & STAGE_BITPACK) cf.header.flags |= FLAG_HAS_BITPACK;
    cf.header.pipeline_config = stages;
    cf.header.original_size = len;
    cf.header.chunk_count = static_cast<uint32_t>(chunks.size());
    cf.header.unique_chunk_count = static_cast<uint32_t>(dedup_result.unique_chunks.size());
    cf.header.original_crc32 = crc32(data, len);

    // Serialize genome
    auto genome_bytes = genome.serialize();
    cf.header.genome_size = static_cast<uint32_t>(genome_bytes.size());
    cf.genome = genome;

    // Chunk references
    cf.chunk_refs = dedup_result.chunk_map;

    // Chunk map entries
    uint64_t offset = 0;
    for (size_t i = 0; i < dedup_result.unique_chunks.size(); i++) {
        ChunkMapEntry entry;
        entry.hash = dedup_result.unique_chunks[i].hash;
        entry.compressed_offset = offset;
        entry.compressed_size = static_cast<uint32_t>(compressed_chunks[i].size());
        entry.original_size = static_cast<uint32_t>(dedup_result.unique_chunks[i].data.size());
        cf.chunk_map.push_back(entry);
        offset += compressed_chunks[i].size();
    }
    cf.header.compressed_payload_size = offset;
    cf.compressed_chunks = std::move(compressed_chunks);

    // Compute payload CRC
    uint32_t pcrc = 0;
    for (auto& cc : cf.compressed_chunks) {
        pcrc = crc32_update(pcrc, cc.data(), cc.size());
    }
    cf.payload_crc32 = pcrc;

    auto result = cf.serialize();
    stats_.compressed_size = result.size();

    std::snprintf(msg_buf, sizeof(msg_buf),
                  "Done: %llu -> %llu bytes (ratio: %.3f)",
                  (unsigned long long)stats_.original_size,
                  (unsigned long long)stats_.compressed_size,
                  stats_.ratio());
    log("result", msg_buf);

    return result;
}

// ============================================================
// Full decompression pipeline
// ============================================================
std::vector<uint8_t> MemAlgoEngine::decompress(const std::vector<uint8_t>& cell_data) {
    return decompress(cell_data.data(), cell_data.size());
}

std::vector<uint8_t> MemAlgoEngine::decompress(const uint8_t* data, size_t len) {
    log("parse", "Parsing .cell file...");
    CellFile cf = CellFile::deserialize(data, len);

    if (cf.header.original_size == 0) {
        return {};
    }

    uint8_t stages = cf.header.pipeline_config;

    // Decompress each unique chunk (parallel when multiple chunks exist)
    log("decompress", "Decompressing chunks...");
    std::vector<std::vector<uint8_t>> decompressed_chunks(cf.header.unique_chunk_count);

    uint32_t num_chunks = cf.header.unique_chunk_count;
    uint32_t hw_threads = std::thread::hardware_concurrency();
    if (hw_threads == 0) hw_threads = 1;
    uint32_t num_threads = std::min(hw_threads, num_chunks);

    if (num_threads <= 1 || num_chunks <= 2) {
        // Sequential fallback for small workloads
        for (uint32_t i = 0; i < num_chunks; i++) {
            decompressed_chunks[i] = decompress_chunk(cf.compressed_chunks[i], stages, cf.genome);
        }
    } else {
        // Parallel decompression — chunks are independent
        auto worker = [&](uint32_t start, uint32_t end) {
            for (uint32_t i = start; i < end; i++) {
                decompressed_chunks[i] = decompress_chunk(
                    cf.compressed_chunks[i], stages, cf.genome);
            }
        };

        std::vector<std::thread> threads;
        uint32_t chunks_per_thread = (num_chunks + num_threads - 1) / num_threads;

        for (uint32_t t = 0; t < num_threads; t++) {
            uint32_t start = t * chunks_per_thread;
            uint32_t end = std::min(start + chunks_per_thread, num_chunks);
            if (start >= end) break;
            threads.emplace_back(worker, start, end);
        }

        for (auto& t : threads) t.join();
    }

    // Reassemble original chunk order using dedup map
    log("reassemble", "Reassembling original data...");
    std::vector<uint8_t> output;
    output.reserve(cf.header.original_size);

    for (uint32_t i = 0; i < cf.header.chunk_count; i++) {
        uint32_t uid = cf.chunk_refs[i];
        if (uid < decompressed_chunks.size()) {
            output.insert(output.end(),
                          decompressed_chunks[uid].begin(),
                          decompressed_chunks[uid].end());
        }
    }

    // Verify CRC32
    uint32_t check_crc = crc32(output.data(), output.size());
    if (check_crc != cf.header.original_crc32) {
        throw std::runtime_error("CRC32 mismatch: data integrity check failed");
    }

    log("verify", "CRC32 verified OK");
    return output;
}

} // namespace memalgo
