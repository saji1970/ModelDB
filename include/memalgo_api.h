/*
 * MemCell Public C API
 * ====================
 * C-compatible API for embedding MemCell compression in applications.
 * Designed for ML model storage/retrieval but works with any data.
 *
 * Usage:
 *   #include "memalgo_api.h"
 *
 *   // Compress
 *   uint8_t* out; size_t out_len;
 *   int rc = memalgo_compress(data, data_len, &out, &out_len, NULL);
 *   // ... use out ...
 *   memalgo_free(out);
 *
 *   // Store/load a model
 *   memalgo_store_model("models/bert.cell", model_data, model_len, NULL);
 *   uint8_t* loaded; size_t loaded_len;
 *   memalgo_load_model("models/bert.cell", &loaded, &loaded_len);
 *   memalgo_free(loaded);
 *
 *   // Model registry
 *   McRegistry* reg = memalgo_registry_open("models/registry.idx");
 *   memalgo_registry_add(reg, "bert-base", "models/bert.cell", model_len, "transformer");
 *   McModelInfo info;
 *   memalgo_registry_find(reg, "bert-base", &info);
 *   memalgo_registry_close(reg);
 */

#ifndef MEMALGO_API_H
#define MEMALGO_API_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ============================================================
 * Error codes
 * ============================================================ */
#define MC_OK                0
#define MC_ERR_INVALID_ARG  -1
#define MC_ERR_IO           -2
#define MC_ERR_CORRUPT      -3
#define MC_ERR_CRC_MISMATCH -4
#define MC_ERR_OOM          -5
#define MC_ERR_NOT_FOUND    -6
#define MC_ERR_INTERNAL     -7

/* Return human-readable error string for an error code */
const char* memalgo_strerror(int errcode);

/* ============================================================
 * Compression options
 * ============================================================ */
typedef struct McCompressOpts {
    int verbose;           /* 0=silent, 1=verbose logging to stderr */
    int compression_level; /* 0=fast (LZ only), 1=default (all stages), 2=max (exhaustive patterns) */
} McCompressOpts;

/* Default options (all zeros = default behavior) */
#define MC_OPTS_DEFAULT {0, 1}

/* ============================================================
 * Core compression / decompression
 * ============================================================ */

/*
 * Compress data in memory.
 * @param input       Input data buffer
 * @param input_len   Input data length in bytes
 * @param output      Pointer to receive allocated output buffer (caller must free with memalgo_free)
 * @param output_len  Pointer to receive output length
 * @param opts        Compression options (NULL for defaults)
 * @return MC_OK on success, negative error code on failure
 */
int memalgo_compress(const uint8_t* input, size_t input_len,
                     uint8_t** output, size_t* output_len,
                     const McCompressOpts* opts);

/*
 * Decompress data in memory.
 * @param input       Compressed .cell data buffer
 * @param input_len   Compressed data length
 * @param output      Pointer to receive allocated output buffer (caller must free with memalgo_free)
 * @param output_len  Pointer to receive original data length
 * @return MC_OK on success, negative error code on failure
 */
int memalgo_decompress(const uint8_t* input, size_t input_len,
                       uint8_t** output, size_t* output_len);

/*
 * Free a buffer allocated by memalgo functions.
 */
void memalgo_free(uint8_t* ptr);

/* ============================================================
 * Compression statistics
 * ============================================================ */
typedef struct McStats {
    uint64_t original_size;
    uint64_t compressed_size;
    double   ratio;             /* compressed/original */
    double   entropy;           /* bits per byte (0-8) */
    uint32_t chunk_count;
    uint32_t unique_chunks;
    uint32_t duplicate_chunks;
    const char* data_type;      /* "text", "binary-structured", etc. */
} McStats;

/*
 * Compress and return statistics (output buffer is optional).
 */
int memalgo_compress_stats(const uint8_t* input, size_t input_len,
                           uint8_t** output, size_t* output_len,
                           McStats* stats,
                           const McCompressOpts* opts);

/* ============================================================
 * File-based model storage
 * ============================================================ */

/*
 * Compress data and write to a .cell file.
 * @param path        Output file path (e.g., "models/bert.cell")
 * @param data        Raw model data
 * @param data_len    Model data length in bytes
 * @param opts        Compression options (NULL for defaults)
 * @return MC_OK on success
 */
int memalgo_store_model(const char* path, const uint8_t* data, size_t data_len,
                        const McCompressOpts* opts);

/*
 * Load and decompress a .cell file.
 * @param path        Input .cell file path
 * @param data        Pointer to receive allocated buffer (free with memalgo_free)
 * @param data_len    Pointer to receive original data length
 * @return MC_OK on success
 */
int memalgo_load_model(const char* path, uint8_t** data, size_t* data_len);

/*
 * Query metadata of a .cell file without decompressing.
 * @param path        Input .cell file path
 * @param stats       Pointer to receive statistics
 * @return MC_OK on success
 */
int memalgo_query_model(const char* path, McStats* stats);

/* ============================================================
 * Streaming API (for large models that don't fit in RAM)
 * ============================================================ */
typedef struct McStream McStream;

/*
 * Open a compression stream writing to a file.
 */
McStream* memalgo_stream_compress_open(const char* output_path, const McCompressOpts* opts);

/*
 * Write data to a compression stream.
 * Can be called multiple times. Data is buffered and compressed in chunks.
 */
int memalgo_stream_write(McStream* stream, const uint8_t* data, size_t len);

/*
 * Finalize and close a compression stream. Flushes all remaining data.
 * @return MC_OK on success
 */
int memalgo_stream_compress_close(McStream* stream);

/*
 * Open a decompression stream reading from a file.
 */
McStream* memalgo_stream_decompress_open(const char* input_path);

/*
 * Read decompressed data from stream.
 * @param buf      Buffer to read into
 * @param buf_len  Buffer capacity
 * @param read_len Pointer to receive actual bytes read (0 = EOF)
 * @return MC_OK on success, MC_ERR_CORRUPT on integrity failure
 */
int memalgo_stream_read(McStream* stream, uint8_t* buf, size_t buf_len, size_t* read_len);

/*
 * Close a decompression stream.
 */
int memalgo_stream_decompress_close(McStream* stream);

/* ============================================================
 * Model Registry
 * Maintains an index of stored models with metadata.
 * ============================================================ */
typedef struct McRegistry McRegistry;

typedef struct McModelInfo {
    char     name[256];           /* Model name / identifier */
    char     path[1024];          /* Path to .cell file */
    char     model_type[128];     /* E.g., "transformer", "cnn", "diffusion" */
    uint64_t original_size;       /* Original uncompressed size */
    uint64_t compressed_size;     /* Compressed .cell file size */
    uint64_t timestamp;           /* Unix timestamp of storage */
    uint32_t version;             /* User-defined version number */
} McModelInfo;

/*
 * Open or create a model registry.
 * @param index_path  Path to the registry index file (e.g., "models/registry.idx")
 * @return Registry handle, or NULL on failure
 */
McRegistry* memalgo_registry_open(const char* index_path);

/*
 * Add or update a model entry in the registry.
 */
int memalgo_registry_add(McRegistry* reg, const char* name, const char* cell_path,
                         uint64_t original_size, const char* model_type, uint32_t version);

/*
 * Find a model by name.
 * @return MC_OK if found, MC_ERR_NOT_FOUND if not
 */
int memalgo_registry_find(McRegistry* reg, const char* name, McModelInfo* info);

/*
 * Remove a model entry from the registry (does NOT delete the .cell file).
 */
int memalgo_registry_remove(McRegistry* reg, const char* name);

/*
 * List all models in the registry.
 * @param entries     Array to fill
 * @param max_entries Capacity of the array
 * @param count       Pointer to receive actual count
 */
int memalgo_registry_list(McRegistry* reg, McModelInfo* entries, size_t max_entries, size_t* count);

/*
 * Close and save the registry.
 */
void memalgo_registry_close(McRegistry* reg);

/* ============================================================
 * Version info
 * ============================================================ */
const char* memalgo_version(void);

#ifdef __cplusplus
}
#endif

#endif /* MEMALGO_API_H */
