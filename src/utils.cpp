#include "utils.h"
#include <fstream>
#include <algorithm>

namespace memalgo {

// ============================================================
// FNV-1a 64-bit
// ============================================================
static constexpr uint64_t FNV64_OFFSET = 14695981039346656037ULL;
static constexpr uint64_t FNV64_PRIME  = 1099511628211ULL;

uint64_t fnv1a_64(const uint8_t* data, size_t len) {
    uint64_t h = FNV64_OFFSET;
    for (size_t i = 0; i < len; i++) {
        h ^= static_cast<uint64_t>(data[i]);
        h *= FNV64_PRIME;
    }
    return h;
}

// ============================================================
// FNV-1a 128-bit (xor-fold from two 64-bit passes with different seeds)
// ============================================================
Hash128 fnv1a_128(const uint8_t* data, size_t len) {
    uint64_t h1 = FNV64_OFFSET;
    uint64_t h2 = FNV64_OFFSET ^ 0x6c62272e07bb0142ULL; // different seed
    for (size_t i = 0; i < len; i++) {
        h1 ^= static_cast<uint64_t>(data[i]);
        h1 *= FNV64_PRIME;
        h2 ^= static_cast<uint64_t>(data[i]);
        h2 *= FNV64_PRIME ^ 0x01000193ULL;
    }
    return Hash128{h1, h2};
}

// ============================================================
// CRC32 (polynomial 0xEDB88320, reflected)
// ============================================================
static uint32_t crc32_table[256];
static bool crc32_table_init = false;

static void init_crc32_table() {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int j = 0; j < 8; j++) {
            if (c & 1)
                c = 0xEDB88320u ^ (c >> 1);
            else
                c >>= 1;
        }
        crc32_table[i] = c;
    }
    crc32_table_init = true;
}

uint32_t crc32_update(uint32_t crc, const uint8_t* data, size_t len) {
    if (!crc32_table_init) init_crc32_table();
    crc = ~crc;
    for (size_t i = 0; i < len; i++) {
        crc = crc32_table[(crc ^ data[i]) & 0xFF] ^ (crc >> 8);
    }
    return ~crc;
}

uint32_t crc32(const uint8_t* data, size_t len) {
    return crc32_update(0, data, len);
}

// ============================================================
// BufferWriter
// ============================================================
void BufferWriter::write_u8(uint8_t v) {
    buf_.push_back(v);
}

void BufferWriter::write_u16(uint16_t v) {
    buf_.push_back(static_cast<uint8_t>(v & 0xFF));
    buf_.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

void BufferWriter::write_u32(uint32_t v) {
    buf_.push_back(static_cast<uint8_t>(v & 0xFF));
    buf_.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
    buf_.push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
    buf_.push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

void BufferWriter::write_u64(uint64_t v) {
    for (int i = 0; i < 8; i++) {
        buf_.push_back(static_cast<uint8_t>((v >> (i * 8)) & 0xFF));
    }
}

void BufferWriter::write_bytes(const uint8_t* data, size_t len) {
    buf_.insert(buf_.end(), data, data + len);
}

void BufferWriter::write_bytes(const std::vector<uint8_t>& data) {
    buf_.insert(buf_.end(), data.begin(), data.end());
}

// ============================================================
// BufferReader
// ============================================================
uint8_t BufferReader::read_u8() {
    check(1);
    return data_[pos_++];
}

uint16_t BufferReader::read_u16() {
    check(2);
    uint16_t v = static_cast<uint16_t>(data_[pos_])
               | (static_cast<uint16_t>(data_[pos_ + 1]) << 8);
    pos_ += 2;
    return v;
}

uint32_t BufferReader::read_u32() {
    check(4);
    uint32_t v = static_cast<uint32_t>(data_[pos_])
               | (static_cast<uint32_t>(data_[pos_ + 1]) << 8)
               | (static_cast<uint32_t>(data_[pos_ + 2]) << 16)
               | (static_cast<uint32_t>(data_[pos_ + 3]) << 24);
    pos_ += 4;
    return v;
}

uint64_t BufferReader::read_u64() {
    check(8);
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) {
        v |= static_cast<uint64_t>(data_[pos_ + i]) << (i * 8);
    }
    pos_ += 8;
    return v;
}

std::vector<uint8_t> BufferReader::read_bytes(size_t n) {
    check(n);
    std::vector<uint8_t> result(data_ + pos_, data_ + pos_ + n);
    pos_ += n;
    return result;
}

void BufferReader::read_bytes_into(uint8_t* dst, size_t n) {
    check(n);
    std::memcpy(dst, data_ + pos_, n);
    pos_ += n;
}

void BufferReader::seek(size_t pos) {
    if (pos > len_)
        throw std::runtime_error("BufferReader: seek past end");
    pos_ = pos;
}

// ============================================================
// File I/O
// ============================================================
std::vector<uint8_t> read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f.is_open())
        throw std::runtime_error("Cannot open file: " + path);
    auto size = f.tellg();
    f.seekg(0, std::ios::beg);
    std::vector<uint8_t> buf(static_cast<size_t>(size));
    if (size > 0) {
        f.read(reinterpret_cast<char*>(buf.data()), size);
    }
    if (!f)
        throw std::runtime_error("Failed to read file: " + path);
    return buf;
}

void write_file(const std::string& path, const std::vector<uint8_t>& data) {
    write_file(path, data.data(), data.size());
}

void write_file(const std::string& path, const uint8_t* data, size_t len) {
    std::ofstream f(path, std::ios::binary);
    if (!f.is_open())
        throw std::runtime_error("Cannot create file: " + path);
    if (len > 0) {
        f.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(len));
    }
    if (!f)
        throw std::runtime_error("Failed to write file: " + path);
}

} // namespace memalgo
