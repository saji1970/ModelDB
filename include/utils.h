#pragma once
#include <cstdint>
#include <cstddef>
#include <vector>
#include <string>
#include <stdexcept>
#include <cstring>

namespace memalgo {

// --- 128-bit hash ---
struct Hash128 {
    uint64_t low;
    uint64_t high;

    bool operator==(const Hash128& o) const { return low == o.low && high == o.high; }
    bool operator!=(const Hash128& o) const { return !(*this == o); }
    bool operator<(const Hash128& o) const {
        return high < o.high || (high == o.high && low < o.low);
    }
};

struct Hash128Hasher {
    size_t operator()(const Hash128& h) const {
        return static_cast<size_t>(h.low ^ (h.high * 0x9e3779b97f4a7c15ULL));
    }
};

// --- FNV-1a hashing ---
uint64_t fnv1a_64(const uint8_t* data, size_t len);
Hash128 fnv1a_128(const uint8_t* data, size_t len);

// --- CRC32 (ISO 3309 / ITU-T V.42) ---
uint32_t crc32(const uint8_t* data, size_t len);
uint32_t crc32_update(uint32_t crc, const uint8_t* data, size_t len);

// --- Buffer Writer (little-endian) ---
class BufferWriter {
public:
    BufferWriter() = default;
    explicit BufferWriter(size_t reserve) { buf_.reserve(reserve); }

    void write_u8(uint8_t v);
    void write_u16(uint16_t v);
    void write_u32(uint32_t v);
    void write_u64(uint64_t v);
    void write_bytes(const uint8_t* data, size_t len);
    void write_bytes(const std::vector<uint8_t>& data);

    const std::vector<uint8_t>& data() const { return buf_; }
    std::vector<uint8_t> take() { return std::move(buf_); }
    size_t size() const { return buf_.size(); }

private:
    std::vector<uint8_t> buf_;
};

// --- Buffer Reader (little-endian) ---
class BufferReader {
public:
    BufferReader(const uint8_t* data, size_t len) : data_(data), len_(len), pos_(0) {}
    BufferReader(const std::vector<uint8_t>& v) : data_(v.data()), len_(v.size()), pos_(0) {}

    uint8_t read_u8();
    uint16_t read_u16();
    uint32_t read_u32();
    uint64_t read_u64();
    std::vector<uint8_t> read_bytes(size_t n);
    void read_bytes_into(uint8_t* dst, size_t n);

    size_t remaining() const { return len_ - pos_; }
    size_t position() const { return pos_; }
    void seek(size_t pos);
    const uint8_t* current_ptr() const { return data_ + pos_; }

private:
    const uint8_t* data_;
    size_t len_;
    size_t pos_;

    void check(size_t n) const {
        if (pos_ + n > len_)
            throw std::runtime_error("BufferReader: read past end");
    }
};

// --- File I/O helpers ---
std::vector<uint8_t> read_file(const std::string& path);
void write_file(const std::string& path, const std::vector<uint8_t>& data);
void write_file(const std::string& path, const uint8_t* data, size_t len);

} // namespace memalgo
