#include "cell.h"
#include <stdexcept>

namespace memalgo {

CellBlock CellBlock::pack(const std::vector<uint8_t>& data, uint8_t bits_per_cell) {
    return pack(data.data(), data.size(), bits_per_cell);
}

CellBlock CellBlock::pack(const uint8_t* data, size_t len, uint8_t bits_per_cell) {
    CellBlock block;
    block.bits_per_cell = bits_per_cell;
    block.original_bits = static_cast<uint32_t>(len * 8);

    if (bits_per_cell == 8) {
        // Direct 1:1 mapping — each byte is one 256-state cell
        block.cells.assign(data, data + len);
        block.cell_count = static_cast<uint32_t>(len);
    } else if (bits_per_cell > 0 && bits_per_cell < 8) {
        // Sub-byte cells: pack multiple cells per byte of input
        // Example: 4 bits/cell means 2 cells per source byte
        uint32_t total_bits = static_cast<uint32_t>(len * 8);
        uint32_t num_cells = (total_bits + bits_per_cell - 1) / bits_per_cell;
        block.cells.resize(num_cells);
        block.cell_count = num_cells;

        uint8_t mask = (1u << bits_per_cell) - 1;
        uint32_t bit_pos = 0;
        for (uint32_t c = 0; c < num_cells; c++) {
            uint32_t byte_idx = bit_pos / 8;
            uint32_t bit_off = bit_pos % 8;

            uint16_t window = 0;
            if (byte_idx < len) window = data[byte_idx];
            if (byte_idx + 1 < len) window |= static_cast<uint16_t>(data[byte_idx + 1]) << 8;

            block.cells[c] = static_cast<uint8_t>((window >> bit_off) & mask);
            bit_pos += bits_per_cell;
        }
    } else if (bits_per_cell > 8 && bits_per_cell <= 16) {
        // Super-byte cells: each cell holds more than 8 bits
        // Store as 2 bytes per cell (little-endian)
        uint32_t total_bits = static_cast<uint32_t>(len * 8);
        uint32_t num_cells = (total_bits + bits_per_cell - 1) / bits_per_cell;
        block.cells.resize(num_cells * 2);
        block.cell_count = num_cells;

        uint16_t mask = (1u << bits_per_cell) - 1;
        uint32_t bit_pos = 0;
        for (uint32_t c = 0; c < num_cells; c++) {
            uint32_t byte_idx = bit_pos / 8;
            uint32_t bit_off = bit_pos % 8;

            uint32_t window = 0;
            for (int b = 0; b < 3 && byte_idx + b < len; b++) {
                window |= static_cast<uint32_t>(data[byte_idx + b]) << (b * 8);
            }

            uint16_t val = static_cast<uint16_t>((window >> bit_off) & mask);
            block.cells[c * 2]     = static_cast<uint8_t>(val & 0xFF);
            block.cells[c * 2 + 1] = static_cast<uint8_t>((val >> 8) & 0xFF);
            bit_pos += bits_per_cell;
        }
    } else {
        throw std::runtime_error("CellBlock: unsupported bits_per_cell");
    }

    return block;
}

std::vector<uint8_t> CellBlock::unpack() const {
    uint32_t total_bytes = (original_bits + 7) / 8;
    std::vector<uint8_t> out(total_bytes, 0);

    if (bits_per_cell == 8) {
        // Direct copy
        size_t n = (cell_count < total_bytes) ? cell_count : total_bytes;
        for (size_t i = 0; i < n; i++) out[i] = cells[i];
    } else if (bits_per_cell > 0 && bits_per_cell < 8) {
        uint32_t bit_pos = 0;
        for (uint32_t c = 0; c < cell_count && bit_pos < original_bits; c++) {
            uint32_t byte_idx = bit_pos / 8;
            uint32_t bit_off = bit_pos % 8;
            if (byte_idx < total_bytes) {
                uint16_t val = static_cast<uint16_t>(cells[c]) << bit_off;
                out[byte_idx] |= static_cast<uint8_t>(val & 0xFF);
                if (byte_idx + 1 < total_bytes) {
                    out[byte_idx + 1] |= static_cast<uint8_t>((val >> 8) & 0xFF);
                }
            }
            bit_pos += bits_per_cell;
        }
    } else if (bits_per_cell > 8 && bits_per_cell <= 16) {
        uint32_t bit_pos = 0;
        for (uint32_t c = 0; c < cell_count && bit_pos < original_bits; c++) {
            uint32_t byte_idx = bit_pos / 8;
            uint32_t bit_off = bit_pos % 8;
            uint16_t val = static_cast<uint16_t>(cells[c * 2])
                         | (static_cast<uint16_t>(cells[c * 2 + 1]) << 8);
            uint32_t wide = static_cast<uint32_t>(val) << bit_off;
            for (int b = 0; b < 3 && byte_idx + b < total_bytes; b++) {
                out[byte_idx + b] |= static_cast<uint8_t>((wide >> (b * 8)) & 0xFF);
            }
            bit_pos += bits_per_cell;
        }
    }

    return out;
}

} // namespace memalgo
