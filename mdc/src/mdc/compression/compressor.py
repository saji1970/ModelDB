"""Pluggable compression (CLAUDE-STORAGE.md section 15).

Only algorithms backed by a library actually present are registered.
LZ4/ZSTD need third-party packages this project doesn't have installed
- section 15 says "do not require external libraries that are not
already available," so selecting them raises
`CompressionNotAvailableError` rather than silently substituting
something else or pretending they work.
"""

from __future__ import annotations

import gzip
import zlib
from abc import ABC, abstractmethod

from mdc.storage_intelligence.strategy_types import CompressionAlgorithm


class CompressionError(Exception):
    """Decompression failed - the physical bytes don't correspond to
    what this algorithm expects (CLAUDE-STORAGE.md section 39)."""


class CompressionNotAvailableError(Exception):
    def __init__(self, algorithm: CompressionAlgorithm):
        super().__init__(f"No compressor available for {algorithm.value} - the required library is not installed")
        self.algorithm = algorithm


class Compressor(ABC):
    @abstractmethod
    def compress(self, data: bytes) -> bytes: ...

    @abstractmethod
    def decompress(self, data: bytes) -> bytes: ...


class NoneCompressor(Compressor):
    def compress(self, data: bytes) -> bytes:
        return data

    def decompress(self, data: bytes) -> bytes:
        return data


class ZlibCompressor(Compressor):
    def compress(self, data: bytes) -> bytes:
        return zlib.compress(data)

    def decompress(self, data: bytes) -> bytes:
        try:
            return zlib.decompress(data)
        except zlib.error as exc:
            raise CompressionError(str(exc)) from exc


class GzipCompressor(Compressor):
    def compress(self, data: bytes) -> bytes:
        return gzip.compress(data)

    def decompress(self, data: bytes) -> bytes:
        try:
            return gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise CompressionError(str(exc)) from exc


_REGISTRY: dict[CompressionAlgorithm, Compressor] = {
    CompressionAlgorithm.NONE: NoneCompressor(),
    CompressionAlgorithm.ZLIB: ZlibCompressor(),
    CompressionAlgorithm.GZIP: GzipCompressor(),
}


def get_compressor(algorithm: CompressionAlgorithm) -> Compressor:
    compressor = _REGISTRY.get(algorithm)
    if compressor is None:
        raise CompressionNotAvailableError(algorithm)
    return compressor


def compress(data: bytes, algorithm: CompressionAlgorithm) -> bytes:
    return get_compressor(algorithm).compress(data)


def decompress(data: bytes, algorithm: CompressionAlgorithm) -> bytes:
    return get_compressor(algorithm).decompress(data)
