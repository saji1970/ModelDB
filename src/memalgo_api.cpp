#include "memalgo_api.h"
#include "memalgo.h"
#include "utils.h"

#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <string>
#include <vector>
#include <fstream>
#include <new>
#include <algorithm>

using namespace memalgo;

// ============================================================
// Version
// ============================================================
const char* memalgo_version(void) {
    return "0.1.0";
}

// ============================================================
// Error strings
// ============================================================
const char* memalgo_strerror(int errcode) {
    switch (errcode) {
        case MC_OK:              return "success";
        case MC_ERR_INVALID_ARG: return "invalid argument";
        case MC_ERR_IO:          return "I/O error";
        case MC_ERR_CORRUPT:     return "corrupt data";
        case MC_ERR_CRC_MISMATCH:return "CRC mismatch";
        case MC_ERR_OOM:         return "out of memory";
        case MC_ERR_NOT_FOUND:   return "not found";
        case MC_ERR_INTERNAL:    return "internal error";
        default:                 return "unknown error";
    }
}

// ============================================================
// Helper: copy vector to malloc'd buffer
// ============================================================
static int vec_to_output(const std::vector<uint8_t>& vec, uint8_t** output, size_t* output_len) {
    if (output) {
        *output = static_cast<uint8_t*>(std::malloc(vec.size()));
        if (!*output) return MC_ERR_OOM;
        std::memcpy(*output, vec.data(), vec.size());
    }
    if (output_len) {
        *output_len = vec.size();
    }
    return MC_OK;
}

// ============================================================
// Core compress / decompress
// ============================================================
int memalgo_compress(const uint8_t* input, size_t input_len,
                     uint8_t** output, size_t* output_len,
                     const McCompressOpts* opts) {
    return memalgo_compress_stats(input, input_len, output, output_len, nullptr, opts);
}

int memalgo_compress_stats(const uint8_t* input, size_t input_len,
                           uint8_t** output, size_t* output_len,
                           McStats* stats,
                           const McCompressOpts* opts) {
    if (!input && input_len > 0) return MC_ERR_INVALID_ARG;

    try {
        MemAlgoEngine engine;

        if (opts && opts->verbose) {
            engine.set_verbose([](const char* stage, const char* msg) {
                std::fprintf(stderr, "  [%s] %s\n", stage, msg);
            });
        }

        std::vector<uint8_t> compressed = engine.compress(input, input_len);

        if (stats) {
            auto& s = engine.last_stats();
            stats->original_size = s.original_size;
            stats->compressed_size = s.compressed_size;
            stats->ratio = s.ratio();
            stats->entropy = s.entropy;
            stats->chunk_count = s.chunk_count;
            stats->unique_chunks = s.unique_chunks;
            stats->duplicate_chunks = s.duplicate_chunks;
            stats->data_type = s.data_type;
        }

        return vec_to_output(compressed, output, output_len);

    } catch (const std::bad_alloc&) {
        return MC_ERR_OOM;
    } catch (const std::exception&) {
        return MC_ERR_INTERNAL;
    }
}

int memalgo_decompress(const uint8_t* input, size_t input_len,
                       uint8_t** output, size_t* output_len) {
    if (!input || input_len == 0) return MC_ERR_INVALID_ARG;

    try {
        MemAlgoEngine engine;
        std::vector<uint8_t> decompressed = engine.decompress(input, input_len);
        return vec_to_output(decompressed, output, output_len);

    } catch (const std::runtime_error& e) {
        std::string msg = e.what();
        if (msg.find("CRC32") != std::string::npos) return MC_ERR_CRC_MISMATCH;
        if (msg.find("Invalid") != std::string::npos) return MC_ERR_CORRUPT;
        return MC_ERR_INTERNAL;
    } catch (const std::bad_alloc&) {
        return MC_ERR_OOM;
    } catch (...) {
        return MC_ERR_INTERNAL;
    }
}

void memalgo_free(uint8_t* ptr) {
    std::free(ptr);
}

// ============================================================
// File-based model storage
// ============================================================
int memalgo_store_model(const char* path, const uint8_t* data, size_t data_len,
                        const McCompressOpts* opts) {
    if (!path || (!data && data_len > 0)) return MC_ERR_INVALID_ARG;

    uint8_t* compressed = nullptr;
    size_t compressed_len = 0;
    int rc = memalgo_compress(data, data_len, &compressed, &compressed_len, opts);
    if (rc != MC_OK) return rc;

    try {
        write_file(std::string(path), compressed, compressed_len);
        memalgo_free(compressed);
        return MC_OK;
    } catch (...) {
        memalgo_free(compressed);
        return MC_ERR_IO;
    }
}

int memalgo_load_model(const char* path, uint8_t** data, size_t* data_len) {
    if (!path || !data || !data_len) return MC_ERR_INVALID_ARG;

    try {
        auto cell_data = read_file(std::string(path));
        return memalgo_decompress(cell_data.data(), cell_data.size(), data, data_len);
    } catch (const std::runtime_error&) {
        return MC_ERR_IO;
    } catch (...) {
        return MC_ERR_INTERNAL;
    }
}

int memalgo_query_model(const char* path, McStats* stats) {
    if (!path || !stats) return MC_ERR_INVALID_ARG;

    try {
        auto cell_data = read_file(std::string(path));
        CellFile cf = CellFile::deserialize(cell_data);

        stats->original_size = cf.header.original_size;
        stats->compressed_size = cell_data.size();
        stats->ratio = (cf.header.original_size > 0)
            ? static_cast<double>(cell_data.size()) / cf.header.original_size : 0.0;
        stats->chunk_count = cf.header.chunk_count;
        stats->unique_chunks = cf.header.unique_chunk_count;
        stats->duplicate_chunks = cf.header.chunk_count - cf.header.unique_chunk_count;
        stats->entropy = 0; // not stored in file
        stats->data_type = "unknown"; // not stored in file

        return MC_OK;
    } catch (const std::runtime_error& e) {
        std::string msg = e.what();
        if (msg.find("Invalid") != std::string::npos) return MC_ERR_CORRUPT;
        return MC_ERR_IO;
    } catch (...) {
        return MC_ERR_INTERNAL;
    }
}

// ============================================================
// Streaming API
// ============================================================
struct McStream {
    enum Mode { COMPRESS, DECOMPRESS };
    Mode mode;
    std::string path;
    std::vector<uint8_t> buffer;
    std::vector<uint8_t> decompressed_data;
    size_t read_pos;
    McCompressOpts opts;
    bool finalized;
};

McStream* memalgo_stream_compress_open(const char* output_path, const McCompressOpts* opts) {
    if (!output_path) return nullptr;
    try {
        McStream* s = new McStream();
        s->mode = McStream::COMPRESS;
        s->path = output_path;
        s->read_pos = 0;
        s->finalized = false;
        if (opts) s->opts = *opts;
        else { s->opts.verbose = 0; s->opts.compression_level = 1; }
        return s;
    } catch (...) {
        return nullptr;
    }
}

int memalgo_stream_write(McStream* stream, const uint8_t* data, size_t len) {
    if (!stream || stream->mode != McStream::COMPRESS || stream->finalized)
        return MC_ERR_INVALID_ARG;
    if (!data && len > 0) return MC_ERR_INVALID_ARG;

    try {
        stream->buffer.insert(stream->buffer.end(), data, data + len);
        return MC_OK;
    } catch (const std::bad_alloc&) {
        return MC_ERR_OOM;
    }
}

int memalgo_stream_compress_close(McStream* stream) {
    if (!stream || stream->mode != McStream::COMPRESS)
        return MC_ERR_INVALID_ARG;

    int rc = memalgo_store_model(stream->path.c_str(),
                                  stream->buffer.data(), stream->buffer.size(),
                                  &stream->opts);
    stream->finalized = true;
    delete stream;
    return rc;
}

McStream* memalgo_stream_decompress_open(const char* input_path) {
    if (!input_path) return nullptr;
    try {
        auto cell_data = read_file(std::string(input_path));

        McStream* s = new McStream();
        s->mode = McStream::DECOMPRESS;
        s->path = input_path;
        s->read_pos = 0;
        s->finalized = false;

        MemAlgoEngine engine;
        s->decompressed_data = engine.decompress(cell_data);

        return s;
    } catch (...) {
        return nullptr;
    }
}

int memalgo_stream_read(McStream* stream, uint8_t* buf, size_t buf_len, size_t* read_len) {
    if (!stream || stream->mode != McStream::DECOMPRESS || !buf || !read_len)
        return MC_ERR_INVALID_ARG;

    size_t remaining = stream->decompressed_data.size() - stream->read_pos;
    size_t to_read = std::min(buf_len, remaining);

    if (to_read > 0) {
        std::memcpy(buf, stream->decompressed_data.data() + stream->read_pos, to_read);
        stream->read_pos += to_read;
    }

    *read_len = to_read;
    return MC_OK;
}

int memalgo_stream_decompress_close(McStream* stream) {
    if (!stream) return MC_ERR_INVALID_ARG;
    delete stream;
    return MC_OK;
}

// ============================================================
// Model Registry
// ============================================================
struct McRegistry {
    std::string index_path;
    std::vector<McModelInfo> entries;
    bool dirty;
};

// Registry file format: simple text-based
// Each line: name|path|model_type|original_size|compressed_size|timestamp|version
static void registry_load(McRegistry* reg) {
    std::ifstream f(reg->index_path);
    if (!f.is_open()) return;

    std::string line;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;

        McModelInfo info = {};
        // Parse pipe-separated fields
        size_t pos = 0;
        int field = 0;
        std::string fields[7];

        for (size_t i = 0; i <= line.size(); i++) {
            if (i == line.size() || line[i] == '|') {
                if (field < 7) fields[field] = line.substr(pos, i - pos);
                field++;
                pos = i + 1;
            }
        }

        if (field < 7) continue;

        std::strncpy(info.name, fields[0].c_str(), sizeof(info.name) - 1);
        std::strncpy(info.path, fields[1].c_str(), sizeof(info.path) - 1);
        std::strncpy(info.model_type, fields[2].c_str(), sizeof(info.model_type) - 1);
        info.original_size = std::strtoull(fields[3].c_str(), nullptr, 10);
        info.compressed_size = std::strtoull(fields[4].c_str(), nullptr, 10);
        info.timestamp = std::strtoull(fields[5].c_str(), nullptr, 10);
        info.version = static_cast<uint32_t>(std::strtoul(fields[6].c_str(), nullptr, 10));

        reg->entries.push_back(info);
    }
}

static void registry_save(McRegistry* reg) {
    std::ofstream f(reg->index_path);
    if (!f.is_open()) return;

    f << "# MemCell Model Registry\n";
    f << "# name|path|model_type|original_size|compressed_size|timestamp|version\n";

    for (auto& e : reg->entries) {
        f << e.name << "|" << e.path << "|" << e.model_type << "|"
          << e.original_size << "|" << e.compressed_size << "|"
          << e.timestamp << "|" << e.version << "\n";
    }
}

McRegistry* memalgo_registry_open(const char* index_path) {
    if (!index_path) return nullptr;
    try {
        McRegistry* reg = new McRegistry();
        reg->index_path = index_path;
        reg->dirty = false;
        registry_load(reg);
        return reg;
    } catch (...) {
        return nullptr;
    }
}

int memalgo_registry_add(McRegistry* reg, const char* name, const char* cell_path,
                         uint64_t original_size, const char* model_type, uint32_t version) {
    if (!reg || !name || !cell_path) return MC_ERR_INVALID_ARG;

    // Check if entry exists — update it
    for (auto& e : reg->entries) {
        if (std::strcmp(e.name, name) == 0) {
            std::strncpy(e.path, cell_path, sizeof(e.path) - 1);
            std::strncpy(e.model_type, model_type ? model_type : "", sizeof(e.model_type) - 1);
            e.original_size = original_size;
            e.version = version;
            e.timestamp = static_cast<uint64_t>(std::time(nullptr));

            // Try to get compressed size from file
            try {
                auto data = read_file(std::string(cell_path));
                e.compressed_size = data.size();
            } catch (...) {
                e.compressed_size = 0;
            }

            reg->dirty = true;
            return MC_OK;
        }
    }

    // New entry
    McModelInfo info = {};
    std::strncpy(info.name, name, sizeof(info.name) - 1);
    std::strncpy(info.path, cell_path, sizeof(info.path) - 1);
    std::strncpy(info.model_type, model_type ? model_type : "", sizeof(info.model_type) - 1);
    info.original_size = original_size;
    info.version = version;
    info.timestamp = static_cast<uint64_t>(std::time(nullptr));

    try {
        auto data = read_file(std::string(cell_path));
        info.compressed_size = data.size();
    } catch (...) {
        info.compressed_size = 0;
    }

    reg->entries.push_back(info);
    reg->dirty = true;
    return MC_OK;
}

int memalgo_registry_find(McRegistry* reg, const char* name, McModelInfo* info) {
    if (!reg || !name || !info) return MC_ERR_INVALID_ARG;

    for (auto& e : reg->entries) {
        if (std::strcmp(e.name, name) == 0) {
            *info = e;
            return MC_OK;
        }
    }
    return MC_ERR_NOT_FOUND;
}

int memalgo_registry_remove(McRegistry* reg, const char* name) {
    if (!reg || !name) return MC_ERR_INVALID_ARG;

    for (auto it = reg->entries.begin(); it != reg->entries.end(); ++it) {
        if (std::strcmp(it->name, name) == 0) {
            reg->entries.erase(it);
            reg->dirty = true;
            return MC_OK;
        }
    }
    return MC_ERR_NOT_FOUND;
}

int memalgo_registry_list(McRegistry* reg, McModelInfo* entries, size_t max_entries, size_t* count) {
    if (!reg || !count) return MC_ERR_INVALID_ARG;

    size_t n = std::min(max_entries, reg->entries.size());
    if (entries && n > 0) {
        std::memcpy(entries, reg->entries.data(), n * sizeof(McModelInfo));
    }
    *count = reg->entries.size();
    return MC_OK;
}

void memalgo_registry_close(McRegistry* reg) {
    if (!reg) return;
    if (reg->dirty) {
        registry_save(reg);
    }
    delete reg;
}
