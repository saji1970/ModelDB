# MemCell

A bio-inspired, multi-stage lossless compression engine optimized for ML model storage. Written in self-contained C++17 with zero external dependencies.

## Overview

MemCell processes data through a 10-stage adaptive pipeline modeled after biological memory systems:

1. **Analyze** - Classify data and select optimal pipeline stages
2. **Chunk** - Content-defined chunking via Buzhash rolling hash
3. **Dedup** - Content-addressable deduplication (FNV-1a 128-bit)
4. **Bitpack** - Pack low-cardinality bytes to minimum bits (1/2/4/6-bit)
5. **Shuffle** - Byte-plane transposition for typed numerical arrays
6. **Pattern Genome** - Codebook replacing frequent 2-8 byte patterns
7. **Delta** - XOR + RLE encoding for similar chunks
8. **LZSS** - Sliding-window compression (64KB window, 258-byte matches)
9. **rANS** - Order-1 context-adaptive entropy coding
10. **Cell Encoding** - Multi-state cell storage + `.cell` file format

The pipeline is adaptive -- each stage is skipped when it wouldn't reduce size.

## Compression Results

| Data Type | Reduction |
|---|---|
| Repetitive text | **69%** |
| Repeated structures | **88%** |
| INT4 quantized weights | **42%** |
| Structured binary | **28%** |
| All zeros | **>99%** |
| Random data | ~0% (expected) |

## Build

```bash
# Windows (MSYS2/MinGW)
build.bat

# Linux/macOS
make

# CMake
mkdir build && cd build && cmake .. && make
```

## Usage

```
memalgo compress   <file> [-o output.cell] [-v]
memalgo decompress <file.cell> [-o output] [-v]
memalgo analyze    <file>
memalgo info       <file.cell>
```

## Run Tests

```bash
make test
# Or directly:
build/memalgo_test
```

## C API

MemCell provides an embeddable C API (`include/memalgo_api.h`):

```c
#include "memalgo_api.h"

// Compress
uint8_t* compressed;
size_t comp_len;
McStats stats;
memalgo_compress_stats(data, size, &compressed, &comp_len, &stats, NULL);
memalgo_free(compressed);

// Store / load models
memalgo_store_model("model.cell", weights, weight_size, NULL);
memalgo_load_model("model.cell", &loaded, &loaded_len);
memalgo_free(loaded);
```

## Project Structure

```
include/       Header files (memalgo.h, analyzer.h, cell.h, etc.)
src/           Implementation files
tests/         Test suite (39 roundtrip tests)
WHITEPAPER.md  Detailed technical whitepaper
```

## License

Open Source
