#pragma once
#include <cstdint>
#include <cstddef>
#include <vector>
#include <string>

namespace memalgo {

// Pipeline stage flags
enum StageFlag : uint8_t {
    STAGE_CHUNKER       = 1 << 0,
    STAGE_DEDUP         = 1 << 1,
    STAGE_PATTERN       = 1 << 2,
    STAGE_DELTA         = 1 << 3,
    STAGE_LZ            = 1 << 4,
    STAGE_ENTROPY       = 1 << 5,
    STAGE_SHUFFLE       = 1 << 6,
    STAGE_BITPACK       = 1 << 7,
    STAGE_ALL_V1        = 0x3F,
    STAGE_ALL           = 0xFF,
};

struct AnalysisResult {
    enum DataType : uint8_t {
        TEXT = 0,
        BINARY_STRUCTURED = 1,
        BINARY_RANDOM = 2,
        ALREADY_COMPRESSED = 3,
    };

    DataType type = BINARY_STRUCTURED;
    double   entropy = 0.0;          // bits per byte (0.0 - 8.0)
    uint64_t file_size = 0;
    uint32_t byte_freq[256] = {};
    uint8_t  recommended_stages = STAGE_ALL;

    // Human-readable type name
    const char* type_name() const;

    // Predicted compression ratio (rough estimate)
    double predicted_ratio() const;
};

// Analyze data to determine type, entropy, and optimal pipeline
AnalysisResult analyze(const uint8_t* data, size_t len);
AnalysisResult analyze(const std::vector<uint8_t>& data);

// Compute Shannon entropy in bits per byte
double shannon_entropy(const uint32_t freq[256], uint64_t total);

} // namespace memalgo
