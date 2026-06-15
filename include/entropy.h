#pragma once
#include <cstdint>
#include <vector>
#include <cstddef>

namespace memalgo {

// rANS (range Asymmetric Numeral Systems) entropy coder.
// Like how neural synapses encode information with varying "weights",
// rANS assigns shorter codes to more frequent symbols and longer codes
// to rare ones, approaching the Shannon entropy limit.

class RANSEncoder {
public:
    // Encode a byte stream using rANS (order-0)
    std::vector<uint8_t> encode(const uint8_t* data, size_t len);
    std::vector<uint8_t> encode(const std::vector<uint8_t>& data);

    // Decode a rANS-encoded stream (order-0)
    std::vector<uint8_t> decode(const uint8_t* data, size_t len);
    std::vector<uint8_t> decode(const std::vector<uint8_t>& data);

    static constexpr uint32_t PROB_BITS = 12;
    static constexpr uint32_t PROB_SCALE = 1u << PROB_BITS; // 4096
    static constexpr uint32_t RANS_L = 1u << 23;            // lower bound of state

    struct SymbolStats {
        uint16_t freq[256];     // quantized frequencies, sum = PROB_SCALE
        uint16_t cum_freq[257]; // cumulative frequencies
    };

    // Build frequency table from data
    static SymbolStats build_stats(const uint8_t* data, size_t len);

    // Quantize raw frequencies to sum to PROB_SCALE
    static void quantize_freqs(uint32_t raw_freq[256], uint16_t out_freq[256], size_t total);
};

// Order-1 context-adaptive rANS encoder.
// Biological analogy: Contextual synaptic weights — neurons adjust
// their response based on recent activity (the previous byte provides
// context for predicting the next byte's distribution).
// Auto-selects between order-0 and order-1 based on which is smaller.

class RANSEncoderOrder1 {
public:
    // Encode with auto order-0/order-1 selection
    // Format: order_flag(1) + [order-0 or order-1 encoded data]
    std::vector<uint8_t> encode(const uint8_t* data, size_t len);
    std::vector<uint8_t> encode(const std::vector<uint8_t>& data);

    // Decode (auto-detects order from the stream)
    std::vector<uint8_t> decode(const uint8_t* data, size_t len);
    std::vector<uint8_t> decode(const std::vector<uint8_t>& data);

private:
    RANSEncoder order0_;

    // Order-1 encode/decode
    std::vector<uint8_t> encode_order1(const uint8_t* data, size_t len);
    std::vector<uint8_t> decode_order1(const uint8_t* data, size_t len);
};

} // namespace memalgo
