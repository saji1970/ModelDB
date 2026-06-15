#pragma once
#include <cstdint>
#include <vector>
#include <cstddef>

namespace memalgo {

// GenomeEntry: A single pattern in the codebook.
// Like a DNA codon mapping to an amino acid, each entry maps
// a frequent byte pattern to a short code.
struct GenomeEntry {
    uint8_t pattern[8];     // the byte pattern (2-8 bytes, was 2-4)
    uint8_t pattern_len;    // length: 2-8
    uint8_t code;           // replacement code 0-254
};

// Genome: The pattern codebook ("DNA" of the data).
// Stores the most frequent multi-byte patterns found in the data
// and maps each to a single-byte code.
struct Genome {
    uint8_t escape_byte = 0;        // escape byte (least frequent in source)
    uint16_t entry_count = 0;
    GenomeEntry entries[256];

    // Serialize genome to bytes
    std::vector<uint8_t> serialize() const;

    // Deserialize genome from bytes
    static Genome deserialize(const uint8_t* data, size_t len);
    static Genome deserialize(const std::vector<uint8_t>& data);
};

class PatternCodec {
public:
    // Build a genome (codebook) by analyzing the data
    Genome build_genome(const uint8_t* data, size_t len);
    Genome build_genome(const std::vector<uint8_t>& data);

    // Encode data using a genome
    std::vector<uint8_t> encode(const uint8_t* data, size_t len, const Genome& genome);
    std::vector<uint8_t> encode(const std::vector<uint8_t>& data, const Genome& genome);

    // Decode data using a genome
    std::vector<uint8_t> decode(const uint8_t* data, size_t len, const Genome& genome);
    std::vector<uint8_t> decode(const std::vector<uint8_t>& data, const Genome& genome);
};

} // namespace memalgo
