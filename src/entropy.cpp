#include "entropy.h"
#include <algorithm>
#include <cstring>
#include <numeric>
#include <stdexcept>

namespace memalgo {

// ============================================================
// Frequency quantization
// ============================================================
void RANSEncoder::quantize_freqs(uint32_t raw_freq[256], uint16_t out_freq[256], size_t total) {
    if (total == 0) {
        for (int i = 0; i < 256; i++) out_freq[i] = PROB_SCALE / 256;
        uint32_t sum = 0;
        for (int i = 0; i < 256; i++) sum += out_freq[i];
        out_freq[0] += static_cast<uint16_t>(PROB_SCALE - sum);
        return;
    }

    uint32_t sum = 0;
    for (int i = 0; i < 256; i++) {
        if (raw_freq[i] > 0) {
            out_freq[i] = static_cast<uint16_t>(
                std::max(1u, static_cast<uint32_t>(
                    (static_cast<uint64_t>(raw_freq[i]) * PROB_SCALE + total / 2) / total
                ))
            );
        } else {
            out_freq[i] = 0;
        }
        sum += out_freq[i];
    }

    int32_t diff = static_cast<int32_t>(PROB_SCALE) - static_cast<int32_t>(sum);
    while (diff != 0) {
        if (diff > 0) {
            int best = -1;
            for (int i = 0; i < 256; i++) {
                if (out_freq[i] > 0 && (best < 0 || out_freq[i] > out_freq[best]))
                    best = i;
            }
            if (best < 0) break;
            out_freq[best]++;
            diff--;
        } else {
            int best = -1;
            for (int i = 0; i < 256; i++) {
                if (out_freq[i] > 1 && (best < 0 || out_freq[i] > out_freq[best]))
                    best = i;
            }
            if (best < 0) break;
            out_freq[best]--;
            diff++;
        }
    }
}

RANSEncoder::SymbolStats RANSEncoder::build_stats(const uint8_t* data, size_t len) {
    SymbolStats stats;
    uint32_t raw_freq[256] = {};
    for (size_t i = 0; i < len; i++) raw_freq[data[i]]++;

    quantize_freqs(raw_freq, stats.freq, len);

    stats.cum_freq[0] = 0;
    for (int i = 0; i < 256; i++) {
        stats.cum_freq[i + 1] = stats.cum_freq[i] + stats.freq[i];
    }

    return stats;
}

// ============================================================
// rANS Encode (order-0, byte-level renormalization)
// ============================================================
std::vector<uint8_t> RANSEncoder::encode(const uint8_t* data, size_t len) {
    if (len == 0) {
        std::vector<uint8_t> out(4, 0);
        return out;
    }

    SymbolStats stats = build_stats(data, len);

    uint32_t state = RANS_L;
    std::vector<uint8_t> output_bytes;
    output_bytes.reserve(len + 16);

    for (size_t i = len; i > 0; i--) {
        uint8_t sym = data[i - 1];
        uint16_t freq = stats.freq[sym];
        uint16_t cum = stats.cum_freq[sym];

        if (freq == 0) continue;

        uint32_t x_max = ((RANS_L >> PROB_BITS) << 8) * freq;
        while (state >= x_max) {
            output_bytes.push_back(static_cast<uint8_t>(state & 0xFF));
            state >>= 8;
        }

        state = ((state / freq) << PROB_BITS) + (state % freq) + cum;
    }

    // Flush final state
    output_bytes.push_back(static_cast<uint8_t>(state & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 8) & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 16) & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 24) & 0xFF));

    // Build output
    std::vector<uint8_t> out;
    size_t header_size = 4 + 512 + 4;
    out.reserve(header_size + output_bytes.size());

    uint32_t orig = static_cast<uint32_t>(len);
    out.push_back(static_cast<uint8_t>(orig & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 24) & 0xFF));

    for (int i = 0; i < 256; i++) {
        out.push_back(static_cast<uint8_t>(stats.freq[i] & 0xFF));
        out.push_back(static_cast<uint8_t>((stats.freq[i] >> 8) & 0xFF));
    }

    uint32_t nbytes = static_cast<uint32_t>(output_bytes.size());
    out.push_back(static_cast<uint8_t>(nbytes & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 24) & 0xFF));

    out.insert(out.end(), output_bytes.begin(), output_bytes.end());

    return out;
}

std::vector<uint8_t> RANSEncoder::encode(const std::vector<uint8_t>& data) {
    return encode(data.data(), data.size());
}

// ============================================================
// rANS Decode (order-0, byte-level renormalization)
// ============================================================
std::vector<uint8_t> RANSEncoder::decode(const uint8_t* data, size_t len) {
    if (len < 4) return {};

    size_t pos = 0;

    uint32_t orig_size = static_cast<uint32_t>(data[pos])
                       | (static_cast<uint32_t>(data[pos + 1]) << 8)
                       | (static_cast<uint32_t>(data[pos + 2]) << 16)
                       | (static_cast<uint32_t>(data[pos + 3]) << 24);
    pos += 4;

    if (orig_size == 0) return {};
    if (pos + 512 + 4 > len) return {};

    SymbolStats stats;
    for (int i = 0; i < 256; i++) {
        stats.freq[i] = static_cast<uint16_t>(data[pos])
                       | (static_cast<uint16_t>(data[pos + 1]) << 8);
        pos += 2;
    }

    stats.cum_freq[0] = 0;
    for (int i = 0; i < 256; i++) {
        stats.cum_freq[i + 1] = stats.cum_freq[i] + stats.freq[i];
    }

    if (stats.cum_freq[256] != PROB_SCALE) return {};

    uint8_t cum_to_sym[PROB_SCALE];
    for (int s = 0; s < 256; s++) {
        for (uint16_t j = stats.cum_freq[s]; j < stats.cum_freq[s + 1]; j++) {
            cum_to_sym[j] = static_cast<uint8_t>(s);
        }
    }

    uint32_t nbytes = static_cast<uint32_t>(data[pos])
                    | (static_cast<uint32_t>(data[pos + 1]) << 8)
                    | (static_cast<uint32_t>(data[pos + 2]) << 16)
                    | (static_cast<uint32_t>(data[pos + 3]) << 24);
    pos += 4;

    if (pos + nbytes > len) return {};

    const uint8_t* enc_bytes = data + pos;

    int32_t bpos = static_cast<int32_t>(nbytes) - 1;
    if (bpos < 3) return {};

    uint32_t state = static_cast<uint32_t>(enc_bytes[bpos]);
    bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]);
    bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]);
    bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]);
    bpos--;

    std::vector<uint8_t> out;
    out.reserve(orig_size);

    for (uint32_t i = 0; i < orig_size; i++) {
        uint32_t slot = state & (PROB_SCALE - 1);
        uint8_t sym = cum_to_sym[slot];
        uint16_t freq = stats.freq[sym];
        uint16_t cum = stats.cum_freq[sym];

        out.push_back(sym);

        state = freq * (state >> PROB_BITS) + slot - cum;

        while (state < RANS_L && bpos >= 0) {
            state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]);
            bpos--;
        }
    }

    return out;
}

std::vector<uint8_t> RANSEncoder::decode(const std::vector<uint8_t>& data) {
    return decode(data.data(), data.size());
}

// ============================================================
// Order-1 Context rANS Encoder
// ============================================================

std::vector<uint8_t> RANSEncoderOrder1::encode_order1(const uint8_t* data, size_t len) {
    // Build 256 context-conditional frequency tables
    uint32_t ctx_freq[256][256] = {};
    uint32_t ctx_total[256] = {};

    // First byte uses context 0
    ctx_freq[0][data[0]]++;
    ctx_total[0]++;
    for (size_t i = 1; i < len; i++) {
        uint8_t ctx = data[i - 1];
        ctx_freq[ctx][data[i]]++;
        ctx_total[ctx]++;
    }

    // Quantize each context's frequencies
    RANSEncoder::SymbolStats ctx_stats[256];
    uint8_t active_contexts[256];
    uint16_t num_active = 0;

    for (int c = 0; c < 256; c++) {
        if (ctx_total[c] > 0) {
            RANSEncoder::quantize_freqs(ctx_freq[c], ctx_stats[c].freq, ctx_total[c]);
            ctx_stats[c].cum_freq[0] = 0;
            for (int i = 0; i < 256; i++) {
                ctx_stats[c].cum_freq[i + 1] = ctx_stats[c].cum_freq[i] + ctx_stats[c].freq[i];
            }
            active_contexts[num_active++] = static_cast<uint8_t>(c);
        }
    }

    // rANS encode with context
    uint32_t state = RANSEncoder::RANS_L;
    std::vector<uint8_t> output_bytes;
    output_bytes.reserve(len + 16);

    for (size_t i = len; i > 0; i--) {
        uint8_t sym = data[i - 1];
        uint8_t ctx = (i >= 2) ? data[i - 2] : 0;

        if (ctx_total[ctx] == 0) continue;

        uint16_t freq = ctx_stats[ctx].freq[sym];
        uint16_t cum = ctx_stats[ctx].cum_freq[sym];

        if (freq == 0) continue;

        uint32_t x_max = ((RANSEncoder::RANS_L >> RANSEncoder::PROB_BITS) << 8) * freq;
        while (state >= x_max) {
            output_bytes.push_back(static_cast<uint8_t>(state & 0xFF));
            state >>= 8;
        }

        state = ((state / freq) << RANSEncoder::PROB_BITS) + (state % freq) + cum;
    }

    // Flush state
    output_bytes.push_back(static_cast<uint8_t>(state & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 8) & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 16) & 0xFF));
    output_bytes.push_back(static_cast<uint8_t>((state >> 24) & 0xFF));

    // Build output: orig_size(4) + num_active(2) + [ctx_byte(1) + freq_table(512)] * num_active + nbytes(4) + encoded
    std::vector<uint8_t> out;
    out.reserve(4 + 2 + num_active * 513 + 4 + output_bytes.size());

    uint32_t orig = static_cast<uint32_t>(len);
    out.push_back(static_cast<uint8_t>(orig & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((orig >> 24) & 0xFF));

    out.push_back(static_cast<uint8_t>(num_active & 0xFF));
    out.push_back(static_cast<uint8_t>((num_active >> 8) & 0xFF));

    for (uint16_t a = 0; a < num_active; a++) {
        uint8_t c = active_contexts[a];
        out.push_back(c);
        for (int i = 0; i < 256; i++) {
            out.push_back(static_cast<uint8_t>(ctx_stats[c].freq[i] & 0xFF));
            out.push_back(static_cast<uint8_t>((ctx_stats[c].freq[i] >> 8) & 0xFF));
        }
    }

    uint32_t nbytes = static_cast<uint32_t>(output_bytes.size());
    out.push_back(static_cast<uint8_t>(nbytes & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 8) & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 16) & 0xFF));
    out.push_back(static_cast<uint8_t>((nbytes >> 24) & 0xFF));

    out.insert(out.end(), output_bytes.begin(), output_bytes.end());

    return out;
}

std::vector<uint8_t> RANSEncoderOrder1::decode_order1(const uint8_t* data, size_t len) {
    if (len < 6) return {};
    size_t pos = 0;

    uint32_t orig_size = static_cast<uint32_t>(data[pos])
                       | (static_cast<uint32_t>(data[pos + 1]) << 8)
                       | (static_cast<uint32_t>(data[pos + 2]) << 16)
                       | (static_cast<uint32_t>(data[pos + 3]) << 24);
    pos += 4;

    if (orig_size == 0) return {};

    uint16_t num_active = static_cast<uint16_t>(data[pos])
                        | (static_cast<uint16_t>(data[pos + 1]) << 8);
    pos += 2;

    // Read context tables
    RANSEncoder::SymbolStats ctx_stats[256];
    bool ctx_valid[256] = {};

    // Build decode lookup tables per context
    // Using heap allocation to avoid stack overflow (256 * 4096 = 1MB)
    std::vector<std::vector<uint8_t>> cum_to_sym(256);

    for (uint16_t a = 0; a < num_active; a++) {
        if (pos + 513 > len) return {};
        uint8_t c = data[pos++];
        ctx_valid[c] = true;

        for (int i = 0; i < 256; i++) {
            ctx_stats[c].freq[i] = static_cast<uint16_t>(data[pos])
                                 | (static_cast<uint16_t>(data[pos + 1]) << 8);
            pos += 2;
        }

        ctx_stats[c].cum_freq[0] = 0;
        for (int i = 0; i < 256; i++) {
            ctx_stats[c].cum_freq[i + 1] = ctx_stats[c].cum_freq[i] + ctx_stats[c].freq[i];
        }

        if (ctx_stats[c].cum_freq[256] != RANSEncoder::PROB_SCALE) return {};

        cum_to_sym[c].resize(RANSEncoder::PROB_SCALE);
        for (int s = 0; s < 256; s++) {
            for (uint16_t j = ctx_stats[c].cum_freq[s]; j < ctx_stats[c].cum_freq[s + 1]; j++) {
                cum_to_sym[c][j] = static_cast<uint8_t>(s);
            }
        }
    }

    if (pos + 4 > len) return {};
    uint32_t nbytes = static_cast<uint32_t>(data[pos])
                    | (static_cast<uint32_t>(data[pos + 1]) << 8)
                    | (static_cast<uint32_t>(data[pos + 2]) << 16)
                    | (static_cast<uint32_t>(data[pos + 3]) << 24);
    pos += 4;

    if (pos + nbytes > len) return {};
    const uint8_t* enc_bytes = data + pos;

    int32_t bpos = static_cast<int32_t>(nbytes) - 1;
    if (bpos < 3) return {};

    uint32_t state = static_cast<uint32_t>(enc_bytes[bpos]); bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]); bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]); bpos--;
    state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]); bpos--;

    std::vector<uint8_t> out;
    out.reserve(orig_size);

    uint8_t prev = 0; // context for first byte

    for (uint32_t i = 0; i < orig_size; i++) {
        uint8_t ctx = prev;
        if (!ctx_valid[ctx]) return out; // shouldn't happen

        uint32_t slot = state & (RANSEncoder::PROB_SCALE - 1);
        uint8_t sym = cum_to_sym[ctx][slot];
        uint16_t freq = ctx_stats[ctx].freq[sym];
        uint16_t cum = ctx_stats[ctx].cum_freq[sym];

        out.push_back(sym);
        prev = sym;

        state = freq * (state >> RANSEncoder::PROB_BITS) + slot - cum;

        while (state < RANSEncoder::RANS_L && bpos >= 0) {
            state = (state << 8) | static_cast<uint32_t>(enc_bytes[bpos]);
            bpos--;
        }
    }

    return out;
}

std::vector<uint8_t> RANSEncoderOrder1::encode(const uint8_t* data, size_t len) {
    // Always try order-0 first
    auto order0_result = order0_.encode(data, len);

    // Only try order-1 for data large enough to justify table overhead (>4KB)
    if (len > 4096) {
        auto order1_result = encode_order1(data, len);

        if (order1_result.size() < order0_result.size()) {
            // Order-1 wins — prepend flag byte 1
            std::vector<uint8_t> out;
            out.reserve(1 + order1_result.size());
            out.push_back(1); // order-1
            out.insert(out.end(), order1_result.begin(), order1_result.end());
            return out;
        }
    }

    // Order-0 wins (or data too small for order-1) — prepend flag byte 0
    std::vector<uint8_t> out;
    out.reserve(1 + order0_result.size());
    out.push_back(0); // order-0
    out.insert(out.end(), order0_result.begin(), order0_result.end());
    return out;
}

std::vector<uint8_t> RANSEncoderOrder1::encode(const std::vector<uint8_t>& data) {
    return encode(data.data(), data.size());
}

std::vector<uint8_t> RANSEncoderOrder1::decode(const uint8_t* data, size_t len) {
    if (len < 1) return {};

    uint8_t order_flag = data[0];
    if (order_flag == 0) {
        return order0_.decode(data + 1, len - 1);
    } else {
        return decode_order1(data + 1, len - 1);
    }
}

std::vector<uint8_t> RANSEncoderOrder1::decode(const std::vector<uint8_t>& data) {
    return decode(data.data(), data.size());
}

} // namespace memalgo
