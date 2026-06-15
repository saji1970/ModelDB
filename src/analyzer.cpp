#include "analyzer.h"
#include <cmath>
#include <algorithm>

namespace memalgo {

double shannon_entropy(const uint32_t freq[256], uint64_t total) {
    if (total == 0) return 0.0;
    double ent = 0.0;
    double inv_total = 1.0 / static_cast<double>(total);
    for (int i = 0; i < 256; i++) {
        if (freq[i] == 0) continue;
        double p = static_cast<double>(freq[i]) * inv_total;
        ent -= p * std::log2(p);
    }
    return ent;
}

const char* AnalysisResult::type_name() const {
    switch (type) {
        case TEXT:               return "text";
        case BINARY_STRUCTURED:  return "binary-structured";
        case BINARY_RANDOM:      return "binary-random";
        case ALREADY_COMPRESSED: return "already-compressed";
    }
    return "unknown";
}

double AnalysisResult::predicted_ratio() const {
    if (entropy <= 0.001) return 0.001; // nearly all same byte
    // Rough prediction: theoretical limit is entropy/8
    // Practical compression reaches ~1.2x theoretical limit
    double theoretical = entropy / 8.0;
    double practical = theoretical * 1.2;
    if (practical > 1.0) practical = 1.0;
    return practical;
}

AnalysisResult analyze(const uint8_t* data, size_t len) {
    AnalysisResult r;
    r.file_size = len;

    if (len == 0) {
        r.type = AnalysisResult::BINARY_STRUCTURED;
        r.entropy = 0.0;
        r.recommended_stages = STAGE_ALL;
        return r;
    }

    // Sample first 64KB for analysis (or full data if smaller)
    size_t sample_len = std::min(len, size_t(65536));

    // Compute byte frequencies
    for (size_t i = 0; i < sample_len; i++) {
        r.byte_freq[data[i]]++;
    }

    // Shannon entropy
    r.entropy = shannon_entropy(r.byte_freq, sample_len);

    // Count distinct bytes used
    int distinct = 0;
    for (int i = 0; i < 256; i++) {
        if (r.byte_freq[i] > 0) distinct++;
    }

    // Count printable ASCII ratio
    uint64_t printable = 0;
    for (int i = 32; i < 127; i++) printable += r.byte_freq[i];
    printable += r.byte_freq['\n'] + r.byte_freq['\r'] + r.byte_freq['\t'];
    double text_ratio = static_cast<double>(printable) / sample_len;

    // Classify
    if (text_ratio > 0.90) {
        r.type = AnalysisResult::TEXT;
    } else if (r.entropy > 7.9 && distinct > 250) {
        // Very high entropy + nearly all byte values used = random/compressed
        r.type = AnalysisResult::ALREADY_COMPRESSED;
    } else if (r.entropy > 7.5) {
        r.type = AnalysisResult::BINARY_RANDOM;
    } else {
        r.type = AnalysisResult::BINARY_STRUCTURED;
    }

    // Recommend pipeline stages based on data type
    switch (r.type) {
        case AnalysisResult::TEXT:
            // Text benefits from all classic stages but not shuffle/bitpack
            r.recommended_stages = STAGE_ALL_V1;
            break;
        case AnalysisResult::BINARY_STRUCTURED:
            // Structured binary gets all stages including shuffle
            r.recommended_stages = STAGE_ALL_V1 | STAGE_SHUFFLE;
            break;
        case AnalysisResult::BINARY_RANDOM:
            // Skip pattern codec and delta — unlikely to help
            r.recommended_stages = STAGE_CHUNKER | STAGE_DEDUP | STAGE_LZ | STAGE_ENTROPY;
            break;
        case AnalysisResult::ALREADY_COMPRESSED:
            // Only chunking and dedup might help (if there are duplicate blocks)
            r.recommended_stages = STAGE_CHUNKER | STAGE_DEDUP;
            break;
    }

    // Enable bit-packing for low-cardinality data (e.g., quantized int4/int8 models)
    if (distinct <= 16 && r.type != AnalysisResult::ALREADY_COMPRESSED) {
        r.recommended_stages |= STAGE_BITPACK;
    }

    return r;
}

AnalysisResult analyze(const std::vector<uint8_t>& data) {
    return analyze(data.data(), data.size());
}

} // namespace memalgo
