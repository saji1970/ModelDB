#include "memalgo.h"
#include <cstdio>
#include <cstring>
#include <string>
#include <chrono>
#include <algorithm>

using namespace memalgo;

static void print_usage() {
    std::fprintf(stderr,
        "MemCell - Bio-Inspired Compression Engine v0.1\n"
        "\n"
        "Usage:\n"
        "  memalgo compress   <input> [-o <output.cell>] [-v]\n"
        "  memalgo decompress <input.cell> [-o <output>] [-v]\n"
        "  memalgo analyze    <input>\n"
        "  memalgo info       <file.cell>\n"
        "\n"
        "Options:\n"
        "  -o <path>   Output file path (default: auto-named)\n"
        "  -v          Verbose: show per-stage compression details\n"
        "\n"
        "Bio-inspired multi-stage compression pipeline:\n"
        "  Analyze -> Chunk -> Deduplicate -> Pattern Genome ->\n"
        "  Delta -> LZSS -> rANS Entropy -> Cell Encoding\n"
    );
}

static std::string default_compress_output(const std::string& input) {
    return input + ".cell";
}

static std::string default_decompress_output(const std::string& input) {
    if (input.size() > 5 && input.substr(input.size() - 5) == ".cell") {
        return input.substr(0, input.size() - 5);
    }
    return input + ".out";
}

static void format_size(uint64_t bytes, char* buf, size_t buf_size) {
    if (bytes < 1024) {
        std::snprintf(buf, buf_size, "%llu B", (unsigned long long)bytes);
    } else if (bytes < 1024 * 1024) {
        std::snprintf(buf, buf_size, "%.2f KB", bytes / 1024.0);
    } else if (bytes < 1024ULL * 1024 * 1024) {
        std::snprintf(buf, buf_size, "%.2f MB", bytes / (1024.0 * 1024));
    } else {
        std::snprintf(buf, buf_size, "%.2f GB", bytes / (1024.0 * 1024 * 1024));
    }
}

// ============================================================
// Commands
// ============================================================

static int cmd_compress(const std::string& input_path, const std::string& output_path, bool verbose) {
    try {
        auto data = read_file(input_path);

        MemAlgoEngine engine;
        if (verbose) {
            engine.set_verbose([](const char* stage, const char* msg) {
                std::fprintf(stderr, "  [%s] %s\n", stage, msg);
            });
        }

        auto t0 = std::chrono::high_resolution_clock::now();
        auto compressed = engine.compress(data);
        auto t1 = std::chrono::high_resolution_clock::now();
        double elapsed = std::chrono::duration<double>(t1 - t0).count();

        write_file(output_path, compressed);

        auto& stats = engine.last_stats();
        char orig_str[64], comp_str[64];
        format_size(stats.original_size, orig_str, sizeof(orig_str));
        format_size(stats.compressed_size, comp_str, sizeof(comp_str));

        std::printf("Compressed: %s -> %s\n", input_path.c_str(), output_path.c_str());
        std::printf("  Original:   %s (%llu bytes)\n", orig_str, (unsigned long long)stats.original_size);
        std::printf("  Compressed: %s (%llu bytes)\n", comp_str, (unsigned long long)stats.compressed_size);
        std::printf("  Ratio:      %.3f (%.1f%%)\n", stats.ratio(), stats.ratio() * 100.0);
        std::printf("  Chunks:     %u total, %u unique, %u duplicates\n",
                     stats.chunk_count, stats.unique_chunks, stats.duplicate_chunks);
        std::printf("  Entropy:    %.2f bits/byte\n", stats.entropy);
        std::printf("  Data type:  %s\n", stats.data_type);
        std::printf("  Time:       %.3f s\n", elapsed);

        if (stats.original_size > 0) {
            double throughput = stats.original_size / elapsed / (1024.0 * 1024.0);
            std::printf("  Throughput: %.1f MB/s\n", throughput);
        }

        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}

static int cmd_decompress(const std::string& input_path, const std::string& output_path, bool verbose) {
    try {
        auto cell_data = read_file(input_path);

        MemAlgoEngine engine;
        if (verbose) {
            engine.set_verbose([](const char* stage, const char* msg) {
                std::fprintf(stderr, "  [%s] %s\n", stage, msg);
            });
        }

        auto t0 = std::chrono::high_resolution_clock::now();
        auto decompressed = engine.decompress(cell_data);
        auto t1 = std::chrono::high_resolution_clock::now();
        double elapsed = std::chrono::duration<double>(t1 - t0).count();

        write_file(output_path, decompressed);

        char size_str[64];
        format_size(decompressed.size(), size_str, sizeof(size_str));
        std::printf("Decompressed: %s -> %s\n", input_path.c_str(), output_path.c_str());
        std::printf("  Size: %s (%llu bytes)\n", size_str, (unsigned long long)decompressed.size());
        std::printf("  Time: %.3f s\n", elapsed);
        std::printf("  CRC32: verified OK\n");

        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}

static int cmd_analyze(const std::string& input_path) {
    try {
        auto data = read_file(input_path);
        auto result = memalgo::analyze(data);

        char size_str[64];
        format_size(result.file_size, size_str, sizeof(size_str));

        std::printf("Analysis: %s\n", input_path.c_str());
        std::printf("  File size:      %s (%llu bytes)\n", size_str, (unsigned long long)result.file_size);
        std::printf("  Data type:      %s\n", result.type_name());
        std::printf("  Entropy:        %.4f bits/byte (max 8.0)\n", result.entropy);
        std::printf("  Predicted ratio: %.3f (%.1f%%)\n", result.predicted_ratio(), result.predicted_ratio() * 100.0);
        std::printf("  Stages:         0x%02X\n", result.recommended_stages);

        // Show byte frequency distribution (top 10 most frequent)
        struct ByteFreq { uint8_t byte; uint32_t freq; };
        ByteFreq sorted[256];
        for (int i = 0; i < 256; i++) {
            sorted[i] = {static_cast<uint8_t>(i), result.byte_freq[i]};
        }
        std::sort(sorted, sorted + 256, [](const ByteFreq& a, const ByteFreq& b) {
            return a.freq > b.freq;
        });

        std::printf("  Top 10 bytes:\n");
        for (int i = 0; i < 10 && sorted[i].freq > 0; i++) {
            uint8_t b = sorted[i].byte;
            double pct = 100.0 * sorted[i].freq / result.file_size;
            if (b >= 32 && b < 127) {
                std::printf("    0x%02X '%c': %u (%.1f%%)\n", b, (char)b, sorted[i].freq, pct);
            } else {
                std::printf("    0x%02X: %u (%.1f%%)\n", b, sorted[i].freq, pct);
            }
        }

        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}

static int cmd_info(const std::string& input_path) {
    try {
        auto data = read_file(input_path);
        auto cf = CellFile::deserialize(data);

        char orig_str[64], comp_str[64];
        format_size(cf.header.original_size, orig_str, sizeof(orig_str));
        format_size(data.size(), comp_str, sizeof(comp_str));

        double ratio = (cf.header.original_size > 0)
            ? static_cast<double>(data.size()) / cf.header.original_size
            : 0.0;

        std::printf("Cell file: %s\n", input_path.c_str());
        std::printf("  Version:       %u\n", cf.header.version);
        std::printf("  Original size: %s (%llu bytes)\n", orig_str, (unsigned long long)cf.header.original_size);
        std::printf("  Cell size:     %s (%llu bytes)\n", comp_str, (unsigned long long)data.size());
        std::printf("  Ratio:         %.3f (%.1f%%)\n", ratio, ratio * 100.0);
        std::printf("  Chunks:        %u total, %u unique\n",
                     cf.header.chunk_count, cf.header.unique_chunk_count);
        std::printf("  Pipeline:      0x%02X\n", cf.header.pipeline_config);
        std::printf("  Flags:         0x%02X", cf.header.flags);
        if (cf.header.flags & FLAG_HAS_GENOME) std::printf(" [genome]");
        if (cf.header.flags & FLAG_HAS_DEDUP)  std::printf(" [dedup]");
        if (cf.header.flags & FLAG_HAS_DELTA)  std::printf(" [delta]");
        std::printf("\n");

        if (cf.header.flags & FLAG_HAS_GENOME) {
            std::printf("  Genome:        %u patterns, escape=0x%02X, %u bytes\n",
                         cf.genome.entry_count, cf.genome.escape_byte, cf.header.genome_size);
        }

        std::printf("  Original CRC:  0x%08X\n", cf.header.original_crc32);
        std::printf("  Payload CRC:   0x%08X\n", cf.payload_crc32);

        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
}

// ============================================================
// Main
// ============================================================
int main(int argc, char* argv[]) {
    if (argc < 3) {
        print_usage();
        return 1;
    }

    std::string command = argv[1];
    std::string input_path = argv[2];
    std::string output_path;
    bool verbose = false;

    // Parse optional arguments
    for (int i = 3; i < argc; i++) {
        if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
            output_path = argv[++i];
        } else if (std::strcmp(argv[i], "-v") == 0) {
            verbose = true;
        }
    }

    if (command == "compress") {
        if (output_path.empty()) output_path = default_compress_output(input_path);
        return cmd_compress(input_path, output_path, verbose);
    } else if (command == "decompress") {
        if (output_path.empty()) output_path = default_decompress_output(input_path);
        return cmd_decompress(input_path, output_path, verbose);
    } else if (command == "analyze") {
        return cmd_analyze(input_path);
    } else if (command == "info") {
        return cmd_info(input_path);
    } else {
        std::fprintf(stderr, "Unknown command: %s\n", command.c_str());
        print_usage();
        return 1;
    }
}
