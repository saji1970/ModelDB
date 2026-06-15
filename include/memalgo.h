#pragma once
#include "utils.h"
#include "analyzer.h"
#include "chunker.h"
#include "dedup.h"
#include "pattern_codec.h"
#include "delta.h"
#include "lz_compress.h"
#include "entropy.h"
#include "shuffle.h"
#include "bitpack.h"
#include "cell.h"
#include "file_format.h"

#include <string>
#include <vector>
#include <functional>

namespace memalgo {

// Compression statistics reported per-stage
struct CompressionStats {
    uint64_t original_size = 0;
    uint64_t compressed_size = 0;
    uint32_t chunk_count = 0;
    uint32_t unique_chunks = 0;
    uint32_t duplicate_chunks = 0;
    double   entropy = 0.0;
    const char* data_type = "unknown";
    uint8_t  stages_used = 0;

    // Per-stage sizes (after each stage)
    uint64_t size_after_dedup = 0;
    uint64_t size_after_pattern = 0;
    uint64_t size_after_delta = 0;
    uint64_t size_after_lz = 0;
    uint64_t size_after_entropy = 0;

    double ratio() const {
        if (original_size == 0) return 0.0;
        return static_cast<double>(compressed_size) / original_size;
    }
};

// Verbose callback type
using VerboseCallback = std::function<void(const char* stage, const char* message)>;

class MemAlgoEngine {
public:
    MemAlgoEngine() = default;

    // Set verbose callback for progress reporting
    void set_verbose(VerboseCallback cb) { verbose_ = cb; }

    // Compress data -> .cell file bytes
    std::vector<uint8_t> compress(const std::vector<uint8_t>& input);
    std::vector<uint8_t> compress(const uint8_t* data, size_t len);

    // Decompress .cell file bytes -> original data
    std::vector<uint8_t> decompress(const std::vector<uint8_t>& cell_data);
    std::vector<uint8_t> decompress(const uint8_t* data, size_t len);

    // Analyze data without compressing
    AnalysisResult analyze(const std::vector<uint8_t>& data);

    // Get stats from last compress/decompress operation
    const CompressionStats& last_stats() const { return stats_; }

private:
    CompressionStats stats_;
    VerboseCallback verbose_;

    void log(const char* stage, const char* msg);

    // Compress a single chunk through the pipeline stages
    std::vector<uint8_t> compress_chunk(const std::vector<uint8_t>& chunk_data,
                                         uint8_t stages, const Genome& genome);

    // Decompress a single chunk back through the pipeline
    std::vector<uint8_t> decompress_chunk(const std::vector<uint8_t>& compressed,
                                           uint8_t stages, const Genome& genome);
};

} // namespace memalgo
