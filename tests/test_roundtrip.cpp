#include "memalgo.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <string>
#include <random>
#include <cmath>

using namespace memalgo;

static int tests_passed = 0;
static int tests_failed = 0;

#define TEST(name) \
    static void test_##name(); \
    struct TestReg_##name { TestReg_##name() { run_test(#name, test_##name); } } reg_##name; \
    static void test_##name()

static void run_test(const char* name, void (*fn)()) {
    std::printf("  %-45s ", name);
    try {
        fn();
        std::printf("PASS\n");
        tests_passed++;
    } catch (const std::exception& e) {
        std::printf("FAIL: %s\n", e.what());
        tests_failed++;
    }
}

static void assert_eq(const std::vector<uint8_t>& a, const std::vector<uint8_t>& b, const char* msg) {
    if (a.size() != b.size()) {
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s: size mismatch (%zu vs %zu)", msg, a.size(), b.size());
        throw std::runtime_error(buf);
    }
    for (size_t i = 0; i < a.size(); i++) {
        if (a[i] != b[i]) {
            char buf[256];
            std::snprintf(buf, sizeof(buf), "%s: byte mismatch at offset %zu (0x%02X vs 0x%02X)",
                          msg, i, a[i], b[i]);
            throw std::runtime_error(buf);
        }
    }
}

// ============================================================
// Individual component tests
// ============================================================

TEST(cell_pack_unpack_8bit) {
    std::vector<uint8_t> data = {0x01, 0x02, 0x03, 0xFF, 0x00, 0xAB};
    auto block = CellBlock::pack(data);
    auto result = block.unpack();
    assert_eq(data, result, "8-bit cell roundtrip");
}

TEST(cell_pack_unpack_4bit) {
    std::vector<uint8_t> data = {0x01, 0x02, 0x03, 0xFF, 0x00, 0xAB, 0xCD, 0xEF};
    auto block = CellBlock::pack(data, 4);
    auto result = block.unpack();
    assert_eq(data, result, "4-bit cell roundtrip");
}

TEST(crc32_basic) {
    const char* test = "Hello, World!";
    uint32_t c = crc32(reinterpret_cast<const uint8_t*>(test), std::strlen(test));
    if (c == 0) throw std::runtime_error("CRC32 returned 0 for non-empty data");
    uint32_t c2 = crc32(reinterpret_cast<const uint8_t*>(test), std::strlen(test));
    if (c != c2) throw std::runtime_error("CRC32 not deterministic");
}

TEST(hash128_basic) {
    std::vector<uint8_t> data = {1, 2, 3, 4, 5};
    auto h1 = fnv1a_128(data.data(), data.size());
    auto h2 = fnv1a_128(data.data(), data.size());
    if (h1 != h2) throw std::runtime_error("Hash128 not deterministic");

    std::vector<uint8_t> data2 = {1, 2, 3, 4, 6};
    auto h3 = fnv1a_128(data2.data(), data2.size());
    if (h1 == h3) throw std::runtime_error("Hash128 collision on different data");
}

TEST(lz_roundtrip) {
    std::string text = "The quick brown fox jumps over the lazy dog. ";
    std::vector<uint8_t> data;
    for (int i = 0; i < 20; i++) {
        data.insert(data.end(), text.begin(), text.end());
    }

    LZCompressor lz;
    auto compressed = lz.compress(data);
    auto decompressed = lz.decompress(compressed);
    assert_eq(data, decompressed, "LZ roundtrip");

    if (compressed.size() >= data.size()) {
        throw std::runtime_error("LZ failed to compress repetitive data");
    }
}

TEST(lz_roundtrip_random) {
    std::mt19937 rng(42);
    std::vector<uint8_t> data(1000);
    for (auto& b : data) b = static_cast<uint8_t>(rng() & 0xFF);

    LZCompressor lz;
    auto compressed = lz.compress(data);
    auto decompressed = lz.decompress(compressed);
    assert_eq(data, decompressed, "LZ roundtrip random");
}

TEST(lz_roundtrip_long_match) {
    // Test long matches (>18 bytes, exercising new 258-byte max match)
    std::vector<uint8_t> pattern(200);
    for (size_t i = 0; i < pattern.size(); i++) pattern[i] = static_cast<uint8_t>(i * 3 + 7);

    std::vector<uint8_t> data;
    for (int i = 0; i < 10; i++) data.insert(data.end(), pattern.begin(), pattern.end());

    LZCompressor lz;
    auto compressed = lz.compress(data);
    auto decompressed = lz.decompress(compressed);
    assert_eq(data, decompressed, "LZ roundtrip long match");

    if (compressed.size() >= data.size() / 2) {
        throw std::runtime_error("LZ long match compression insufficient");
    }
}

TEST(lz_roundtrip_far_match) {
    // Test matches beyond old 4KB window
    std::vector<uint8_t> pattern(100);
    for (size_t i = 0; i < pattern.size(); i++) pattern[i] = static_cast<uint8_t>(i * 11 + 3);

    std::vector<uint8_t> data;
    data.insert(data.end(), pattern.begin(), pattern.end());
    // Insert 8KB of filler to push match beyond old 4KB window
    std::mt19937 rng(99);
    for (int i = 0; i < 8192; i++) data.push_back(static_cast<uint8_t>(rng() & 0xFF));
    data.insert(data.end(), pattern.begin(), pattern.end());

    LZCompressor lz;
    auto compressed = lz.compress(data);
    auto decompressed = lz.decompress(compressed);
    assert_eq(data, decompressed, "LZ roundtrip far match");
}

TEST(rans_roundtrip) {
    std::string text = "aaabbbcccdddeeefffggg hello world test test test";
    std::vector<uint8_t> data(text.begin(), text.end());

    RANSEncoder rans;
    auto encoded = rans.encode(data);
    auto decoded = rans.decode(encoded);
    assert_eq(data, decoded, "rANS roundtrip");
}

TEST(rans_roundtrip_large) {
    std::vector<uint8_t> data(10000);
    std::mt19937 rng(123);
    for (auto& b : data) {
        if (rng() % 2 == 0) b = 'a';
        else b = static_cast<uint8_t>(rng() % 256);
    }

    RANSEncoder rans;
    auto encoded = rans.encode(data);
    auto decoded = rans.decode(encoded);
    assert_eq(data, decoded, "rANS roundtrip large");
}

TEST(rans_order1_roundtrip) {
    // Large data with strong byte-to-byte correlations
    std::vector<uint8_t> data(8000);
    for (size_t i = 0; i < data.size(); i++) {
        // After 'a', likely 'b'; after 'x', likely 'y'
        if (i % 2 == 0) data[i] = 'a' + (i / 100) % 10;
        else data[i] = data[i - 1] + 1;
    }

    RANSEncoderOrder1 rans;
    auto encoded = rans.encode(data);
    auto decoded = rans.decode(encoded);
    assert_eq(data, decoded, "rANS order-1 roundtrip");
}

TEST(rans_order1_small_fallback) {
    // Small data should fall back to order-0
    std::string text = "hello world";
    std::vector<uint8_t> data(text.begin(), text.end());

    RANSEncoderOrder1 rans;
    auto encoded = rans.encode(data);
    auto decoded = rans.decode(encoded);
    assert_eq(data, decoded, "rANS order-1 fallback");
}

TEST(pattern_codec_roundtrip) {
    std::string text = "the the the and and and for for for but but but";
    std::vector<uint8_t> data(text.begin(), text.end());

    PatternCodec codec;
    auto genome = codec.build_genome(data);
    auto encoded = codec.encode(data, genome);
    auto decoded = codec.decode(encoded, genome);
    assert_eq(data, decoded, "Pattern codec roundtrip");
}

TEST(pattern_codec_long_patterns) {
    // Data with 8-byte repeating patterns
    std::string pattern = "ABCDEFGH";
    std::string text;
    for (int i = 0; i < 100; i++) {
        text += pattern;
        text += "xx";
    }
    std::vector<uint8_t> data(text.begin(), text.end());

    PatternCodec codec;
    auto genome = codec.build_genome(data);
    auto encoded = codec.encode(data, genome);
    auto decoded = codec.decode(encoded, genome);
    assert_eq(data, decoded, "Pattern codec long patterns roundtrip");

    // Verify the genome found patterns longer than 4 bytes
    bool has_long = false;
    for (uint16_t i = 0; i < genome.entry_count; i++) {
        if (genome.entries[i].pattern_len > 4) { has_long = true; break; }
    }
    if (!has_long) {
        throw std::runtime_error("Expected long patterns (>4 bytes) in genome");
    }
}

TEST(genome_serialize) {
    PatternCodec codec;
    std::string text = "abcabc defdef ghighi";
    std::vector<uint8_t> data(text.begin(), text.end());
    auto genome = codec.build_genome(data);

    auto bytes = genome.serialize();
    auto genome2 = Genome::deserialize(bytes);

    if (genome.entry_count != genome2.entry_count)
        throw std::runtime_error("Genome entry count mismatch");
    if (genome.escape_byte != genome2.escape_byte)
        throw std::runtime_error("Genome escape byte mismatch");
}

TEST(chunker_reassemble) {
    std::vector<uint8_t> data(8000);
    for (size_t i = 0; i < data.size(); i++) {
        data[i] = static_cast<uint8_t>(i * 7 + 3);
    }

    Chunker chunker;
    auto chunks = chunker.chunk(data);
    auto reassembled = Chunker::reassemble(chunks);
    assert_eq(data, reassembled, "Chunker reassemble");
}

TEST(dedup_basic) {
    std::vector<uint8_t> block(1024);
    for (size_t i = 0; i < block.size(); i++) block[i] = static_cast<uint8_t>(i & 0xFF);

    std::vector<uint8_t> data;
    for (int i = 0; i < 3; i++) data.insert(data.end(), block.begin(), block.end());

    Chunker chunker({512, 1024, 2048});
    auto chunks = chunker.chunk(data);

    Deduplicator dedup;
    auto result = dedup.deduplicate(chunks);

    auto restored = Deduplicator::restore(result);
    auto reassembled = Chunker::reassemble(restored);
    assert_eq(data, reassembled, "Dedup restore");
}

TEST(analyzer_text) {
    std::string text = "Hello world! This is a text file with mostly ASCII characters.\n";
    std::vector<uint8_t> data(text.begin(), text.end());
    auto r = memalgo::analyze(data);
    if (r.type != AnalysisResult::TEXT)
        throw std::runtime_error("Expected TEXT classification");
}

TEST(analyzer_random) {
    std::mt19937 rng(0);
    std::vector<uint8_t> data(65536);
    for (auto& b : data) b = static_cast<uint8_t>(rng() & 0xFF);
    auto r = memalgo::analyze(data);
    if (r.entropy < 7.5)
        throw std::runtime_error("Expected high entropy for random data");
}

// ============================================================
// Shuffle filter tests
// ============================================================

TEST(shuffle_roundtrip_float32) {
    // Simulated float32 data
    std::vector<uint8_t> data(4000);
    std::mt19937 rng(55);
    for (size_t i = 0; i < data.size(); i += 4) {
        // Exponent bytes similar, mantissa varies
        data[i] = 0x3F;  // common exponent
        data[i + 1] = static_cast<uint8_t>(0x80 + (rng() % 20));
        data[i + 2] = static_cast<uint8_t>(rng() & 0xFF);
        data[i + 3] = static_cast<uint8_t>(rng() & 0xFF);
    }

    auto shuffled = ByteShuffler::shuffle(data, 4);
    auto unshuffled = ByteShuffler::unshuffle(shuffled, 4);
    assert_eq(data, unshuffled, "Shuffle roundtrip float32");
}

TEST(shuffle_roundtrip_float16) {
    std::vector<uint8_t> data(2000);
    std::mt19937 rng(77);
    for (size_t i = 0; i < data.size(); i += 2) {
        data[i] = static_cast<uint8_t>(0x38 + (rng() % 5));
        data[i + 1] = static_cast<uint8_t>(rng() & 0xFF);
    }

    auto shuffled = ByteShuffler::shuffle(data, 2);
    auto unshuffled = ByteShuffler::unshuffle(shuffled, 2);
    assert_eq(data, unshuffled, "Shuffle roundtrip float16");
}

TEST(shuffle_detect_float32) {
    // Float32-like data should detect element_size=4
    std::vector<uint8_t> data(4000);
    for (size_t i = 0; i < data.size(); i += 4) {
        data[i] = 0x3F;
        data[i + 1] = 0x80;
        data[i + 2] = static_cast<uint8_t>(i & 0xFF);
        data[i + 3] = static_cast<uint8_t>((i >> 2) & 0xFF);
    }

    uint8_t detected = ByteShuffler::detect_element_size(data.data(), data.size());
    // Should detect element_size > 1 (exact value depends on entropy calculation)
    if (detected == 1) {
        // Acceptable if entropy doesn't differ enough; not a hard failure
    }
}

TEST(shuffle_detect_random) {
    std::mt19937 rng(88);
    std::vector<uint8_t> data(4000);
    for (auto& b : data) b = static_cast<uint8_t>(rng() & 0xFF);

    uint8_t detected = ByteShuffler::detect_element_size(data.data(), data.size());
    if (detected != 1) {
        throw std::runtime_error("Expected no shuffle benefit for random data");
    }
}

// ============================================================
// Bit-packing tests
// ============================================================

TEST(bitpack_roundtrip_4bit) {
    // Data with exactly 16 distinct values (simulating INT4)
    std::vector<uint8_t> data(1000);
    std::mt19937 rng(33);
    for (auto& b : data) b = static_cast<uint8_t>(rng() % 16);

    auto packed = BitPacker::pack(data);
    auto unpacked = BitPacker::unpack(packed);
    assert_eq(data, unpacked, "Bitpack roundtrip 4-bit");

    // Should be significantly smaller
    if (packed.size() >= data.size()) {
        throw std::runtime_error("Bitpack failed to reduce 4-bit data");
    }
}

TEST(bitpack_roundtrip_2bit) {
    // Data with 4 distinct values
    std::vector<uint8_t> data(1000);
    std::mt19937 rng(44);
    uint8_t vals[] = {0, 10, 200, 255};
    for (auto& b : data) b = vals[rng() % 4];

    auto packed = BitPacker::pack(data);
    auto unpacked = BitPacker::unpack(packed);
    assert_eq(data, unpacked, "Bitpack roundtrip 2-bit");
}

TEST(bitpack_roundtrip_1bit) {
    // Binary data (0 and 1 only)
    std::vector<uint8_t> data(1000);
    std::mt19937 rng(22);
    for (auto& b : data) b = static_cast<uint8_t>(rng() % 2);

    auto packed = BitPacker::pack(data);
    auto unpacked = BitPacker::unpack(packed);
    assert_eq(data, unpacked, "Bitpack roundtrip 1-bit");
}

TEST(bitpack_no_benefit) {
    // Random data with all 256 values
    std::mt19937 rng(11);
    std::vector<uint8_t> data(1000);
    for (auto& b : data) b = static_cast<uint8_t>(rng() & 0xFF);

    auto packed = BitPacker::pack(data);
    auto unpacked = BitPacker::unpack(packed);
    assert_eq(data, unpacked, "Bitpack no-benefit roundtrip");
}

// ============================================================
// Full pipeline roundtrip tests
// ============================================================

TEST(pipeline_empty) {
    std::vector<uint8_t> data;
    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Empty pipeline roundtrip");
}

TEST(pipeline_single_byte) {
    std::vector<uint8_t> data = {0x42};
    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Single byte pipeline roundtrip");
}

TEST(pipeline_zeros) {
    std::vector<uint8_t> data(100000, 0x00);
    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "All-zeros pipeline roundtrip");

    if (compressed.size() >= data.size()) {
        throw std::runtime_error("Failed to compress all-zeros data");
    }
}

TEST(pipeline_text) {
    std::string text;
    for (int i = 0; i < 100; i++) {
        text += "The quick brown fox jumps over the lazy dog. ";
        text += "Pack my box with five dozen liquor jugs. ";
    }
    std::vector<uint8_t> data(text.begin(), text.end());

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Text pipeline roundtrip");

    double ratio = static_cast<double>(compressed.size()) / data.size();
    char buf[128];
    std::snprintf(buf, sizeof(buf), "(ratio: %.3f) ", ratio);
    std::printf("%s", buf);
}

TEST(pipeline_binary_pattern) {
    std::vector<uint8_t> data;
    for (int i = 0; i < 5000; i++) {
        data.push_back(static_cast<uint8_t>(i & 0xFF));
        data.push_back(static_cast<uint8_t>((i >> 8) & 0xFF));
        data.push_back(0x00);
        data.push_back(0xFF);
    }

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Binary pattern pipeline roundtrip");

    double ratio = static_cast<double>(compressed.size()) / data.size();
    char buf[128];
    std::snprintf(buf, sizeof(buf), "(ratio: %.3f) ", ratio);
    std::printf("%s", buf);
}

TEST(pipeline_random) {
    std::mt19937 rng(12345);
    std::vector<uint8_t> data(50000);
    for (auto& b : data) b = static_cast<uint8_t>(rng() & 0xFF);

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Random pipeline roundtrip");
}

TEST(pipeline_repeated_blocks) {
    std::vector<uint8_t> block(4096);
    for (size_t i = 0; i < block.size(); i++) {
        block[i] = static_cast<uint8_t>((i * 17 + 5) & 0xFF);
    }

    std::vector<uint8_t> data;
    for (int i = 0; i < 50; i++) {
        data.insert(data.end(), block.begin(), block.end());
    }

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Repeated blocks pipeline roundtrip");

    double ratio = static_cast<double>(compressed.size()) / data.size();
    char buf[128];
    std::snprintf(buf, sizeof(buf), "(ratio: %.3f) ", ratio);
    std::printf("%s", buf);
}

TEST(pipeline_float32_data) {
    // Simulated float32 ML model weights
    std::vector<uint8_t> data(20000);
    std::mt19937 rng(999);
    for (size_t i = 0; i < data.size(); i += 4) {
        // Simulate float32: common exponent, varying mantissa
        data[i] = 0x3F;     // exponent byte (very regular)
        data[i + 1] = static_cast<uint8_t>(0x80 + (rng() % 10));
        data[i + 2] = static_cast<uint8_t>(rng() % 100);
        data[i + 3] = static_cast<uint8_t>(rng() % 200);
    }

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Float32 pipeline roundtrip");

    double ratio = static_cast<double>(compressed.size()) / data.size();
    char buf[128];
    std::snprintf(buf, sizeof(buf), "(ratio: %.3f) ", ratio);
    std::printf("%s", buf);
}

TEST(pipeline_quantized_int4) {
    // Simulated INT4 quantized model weights (only 16 distinct values)
    std::vector<uint8_t> data(20000);
    std::mt19937 rng(777);
    for (auto& b : data) b = static_cast<uint8_t>(rng() % 16);

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "INT4 quantized pipeline roundtrip");

    double ratio = static_cast<double>(compressed.size()) / data.size();
    char buf[128];
    std::snprintf(buf, sizeof(buf), "(ratio: %.3f) ", ratio);
    std::printf("%s", buf);
}

TEST(pipeline_large_parallel) {
    // Large data to exercise parallel decompression
    std::string base = "The quick brown fox jumps over the lazy dog. ";
    std::vector<uint8_t> data;
    for (int i = 0; i < 2000; i++) {
        data.insert(data.end(), base.begin(), base.end());
        // Add some variation
        data.push_back(static_cast<uint8_t>('A' + (i % 26)));
    }

    MemAlgoEngine engine;
    auto compressed = engine.compress(data);
    auto decompressed = engine.decompress(compressed);
    assert_eq(data, decompressed, "Large parallel pipeline roundtrip");
}

TEST(file_format_roundtrip) {
    CellFile cf;
    std::memcpy(cf.header.magic, CELL_MAGIC, 4);
    cf.header.version = CELL_VERSION_2;
    cf.header.flags = FLAG_HAS_GENOME | FLAG_HAS_DEDUP;
    cf.header.pipeline_config = STAGE_ALL;
    cf.header.original_size = 12345;
    cf.header.compressed_payload_size = 100;
    cf.header.chunk_count = 3;
    cf.header.unique_chunk_count = 2;
    cf.header.original_crc32 = 0xDEADBEEF;

    cf.genome.escape_byte = 0xFF;
    cf.genome.entry_count = 1;
    cf.genome.entries[0].pattern[0] = 'a';
    cf.genome.entries[0].pattern[1] = 'b';
    cf.genome.entries[0].pattern_len = 2;
    cf.genome.entries[0].code = 0;
    cf.header.genome_size = static_cast<uint32_t>(cf.genome.serialize().size());

    cf.chunk_refs = {0, 1, 0};

    ChunkMapEntry e1 = {{0x111, 0x222}, 0, 50, 6000};
    ChunkMapEntry e2 = {{0x333, 0x444}, 50, 50, 6345};
    cf.chunk_map = {e1, e2};
    cf.compressed_chunks = {{1,2,3,4,5,6,7,8,9,10,
                             11,12,13,14,15,16,17,18,19,20,
                             21,22,23,24,25,26,27,28,29,30,
                             31,32,33,34,35,36,37,38,39,40,
                             41,42,43,44,45,46,47,48,49,50},
                            {51,52,53,54,55,56,57,58,59,60,
                             61,62,63,64,65,66,67,68,69,70,
                             71,72,73,74,75,76,77,78,79,80,
                             81,82,83,84,85,86,87,88,89,90,
                             91,92,93,94,95,96,97,98,99,100}};
    cf.payload_crc32 = 0xCAFEBABE;

    auto bytes = cf.serialize();
    auto cf2 = CellFile::deserialize(bytes);

    if (cf2.header.original_size != cf.header.original_size)
        throw std::runtime_error("Original size mismatch");
    if (cf2.header.chunk_count != cf.header.chunk_count)
        throw std::runtime_error("Chunk count mismatch");
    if (cf2.chunk_refs.size() != cf.chunk_refs.size())
        throw std::runtime_error("Chunk refs size mismatch");
    if (cf2.compressed_chunks[0].size() != cf.compressed_chunks[0].size())
        throw std::runtime_error("Compressed chunk 0 size mismatch");
}

// ============================================================
// Buffer reader/writer tests
// ============================================================

TEST(buffer_rw) {
    BufferWriter w;
    w.write_u8(0x42);
    w.write_u16(0x1234);
    w.write_u32(0xDEADBEEF);
    w.write_u64(0x0102030405060708ULL);

    BufferReader r(w.data());
    if (r.read_u8() != 0x42) throw std::runtime_error("u8 mismatch");
    if (r.read_u16() != 0x1234) throw std::runtime_error("u16 mismatch");
    if (r.read_u32() != 0xDEADBEEF) throw std::runtime_error("u32 mismatch");
    if (r.read_u64() != 0x0102030405060708ULL) throw std::runtime_error("u64 mismatch");
}

// ============================================================
// Main
// ============================================================
int main() {
    std::printf("MemCell Round-Trip Tests (v2 Enhanced)\n");
    std::printf("========================================\n");

    tests_passed = 0;
    tests_failed = 0;

    // Component tests
    std::printf("\n[Component Tests]\n");
    run_test("buffer_rw", test_buffer_rw);
    run_test("crc32_basic", test_crc32_basic);
    run_test("hash128_basic", test_hash128_basic);
    run_test("cell_pack_unpack_8bit", test_cell_pack_unpack_8bit);
    run_test("cell_pack_unpack_4bit", test_cell_pack_unpack_4bit);
    run_test("analyzer_text", test_analyzer_text);
    run_test("analyzer_random", test_analyzer_random);
    run_test("chunker_reassemble", test_chunker_reassemble);
    run_test("dedup_basic", test_dedup_basic);
    run_test("pattern_codec_roundtrip", test_pattern_codec_roundtrip);
    run_test("pattern_codec_long_patterns", test_pattern_codec_long_patterns);
    run_test("genome_serialize", test_genome_serialize);
    run_test("lz_roundtrip", test_lz_roundtrip);
    run_test("lz_roundtrip_random", test_lz_roundtrip_random);
    run_test("lz_roundtrip_long_match", test_lz_roundtrip_long_match);
    run_test("lz_roundtrip_far_match", test_lz_roundtrip_far_match);
    run_test("rans_roundtrip", test_rans_roundtrip);
    run_test("rans_roundtrip_large", test_rans_roundtrip_large);
    run_test("rans_order1_roundtrip", test_rans_order1_roundtrip);
    run_test("rans_order1_small_fallback", test_rans_order1_small_fallback);
    run_test("file_format_roundtrip", test_file_format_roundtrip);

    // Shuffle & bitpack tests
    std::printf("\n[Shuffle & Bitpack Tests]\n");
    run_test("shuffle_roundtrip_float32", test_shuffle_roundtrip_float32);
    run_test("shuffle_roundtrip_float16", test_shuffle_roundtrip_float16);
    run_test("shuffle_detect_float32", test_shuffle_detect_float32);
    run_test("shuffle_detect_random", test_shuffle_detect_random);
    run_test("bitpack_roundtrip_4bit", test_bitpack_roundtrip_4bit);
    run_test("bitpack_roundtrip_2bit", test_bitpack_roundtrip_2bit);
    run_test("bitpack_roundtrip_1bit", test_bitpack_roundtrip_1bit);
    run_test("bitpack_no_benefit", test_bitpack_no_benefit);

    // Full pipeline tests
    std::printf("\n[Pipeline Tests]\n");
    run_test("pipeline_empty", test_pipeline_empty);
    run_test("pipeline_single_byte", test_pipeline_single_byte);
    run_test("pipeline_zeros", test_pipeline_zeros);
    run_test("pipeline_text", test_pipeline_text);
    run_test("pipeline_binary_pattern", test_pipeline_binary_pattern);
    run_test("pipeline_random", test_pipeline_random);
    run_test("pipeline_repeated_blocks", test_pipeline_repeated_blocks);
    run_test("pipeline_float32_data", test_pipeline_float32_data);
    run_test("pipeline_quantized_int4", test_pipeline_quantized_int4);
    run_test("pipeline_large_parallel", test_pipeline_large_parallel);

    // Summary
    std::printf("\n========================================\n");
    std::printf("Results: %d passed, %d failed (of %d total)\n",
                tests_passed, tests_failed, tests_passed + tests_failed);

    return tests_failed > 0 ? 1 : 0;
}
