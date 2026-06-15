#pragma once
#include <cstdint>
#include <vector>

namespace memalgo {

// CellBlock: Bio-inspired multi-state storage unit.
// Each "cell" can hold N states (default 256 = 8 bits per cell).
// This abstraction models biological memory cells that store information
// in multiple states rather than simple binary on/off.
// Future expansion: higher-radix cells (base-65536 = 16 bits, etc.)
struct CellBlock {
    std::vector<uint8_t> cells;   // raw cell data
    uint32_t cell_count = 0;      // number of cells
    uint8_t  bits_per_cell = 8;   // states = 2^bits_per_cell
    uint32_t original_bits = 0;   // total original bits packed

    // Pack a byte stream into cells
    static CellBlock pack(const std::vector<uint8_t>& data, uint8_t bits_per_cell = 8);
    static CellBlock pack(const uint8_t* data, size_t len, uint8_t bits_per_cell = 8);

    // Unpack cells back to byte stream
    std::vector<uint8_t> unpack() const;

    // Get compression metadata
    size_t storage_size() const { return cells.size(); }
    double density() const {
        if (cells.empty()) return 0.0;
        return static_cast<double>(original_bits) / (cells.size() * bits_per_cell);
    }
};

} // namespace memalgo
