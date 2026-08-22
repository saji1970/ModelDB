"""DataClassifier (CLAUDE-STORAGE.md sections 4-6).

Deterministic, offline - no LLM involved (section 4: "The classifier
must be deterministic initially"). Order of checks matters and mirrors
section 6's priority: strong binary signatures first (a JPEG is a JPEG
regardless of its extension), then the handful of formats without a
cheap reliable signature (ONNX/legacy .pt - extension only, documented
below), then structural text sniffing, then a last-resort fallback.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mdc.classification import detector
from mdc.classification.data_type import DataType
from mdc.classification.metadata import DataProfile, build_profile

# ONNX is protobuf with no cheap, reliable magic-byte signature; legacy
# (pre-1.6) PyTorch checkpoints are raw pickle, not the ZIP container
# `detector.detect_zip_payload` can inspect. Both fall back to
# extension alone - an honest, documented gap (section 6), not silently
# treated as equivalent to a verified content signature.
_EXTENSION_ONLY_MODEL_FORMATS = {".onnx", ".pt", ".pth", ".ckpt"}
_DOCUMENT_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".rst"}

_LOG_LINE_PATTERN = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


class DataClassifier:
    def classify(self, content: bytes, filename: str | None = None) -> DataType:
        if not content:
            return DataType.UNKNOWN

        for detect in (detector.detect_image, detector.detect_audio, detector.detect_video, detector.detect_document):
            signature = detect(content)
            if signature is not None:
                return DataType(signature.data_type)

        gguf = detector.detect_gguf(content)
        if gguf is not None:
            return DataType.AI_MODEL

        safetensors = detector.detect_safetensors(content)
        if safetensors is not None:
            return DataType.AI_MODEL

        if detector.detect_numpy_tensor(content) is not None:
            return DataType.TENSOR

        if detector.detect_parquet(content) is not None:
            return DataType.TABULAR

        archive = detector.detect_archive_container(content)
        if archive is not None:
            if archive.detail == "zip":
                zip_payload = detector.detect_zip_payload(content)
                if zip_payload is not None:
                    return DataType(zip_payload.data_type)
            return DataType.ARCHIVE

        extension = Path(filename).suffix.lower() if filename else ""
        if extension in _EXTENSION_ONLY_MODEL_FORMATS:
            return DataType.AI_MODEL

        text = _try_decode(content)
        if text is not None:
            return self._classify_text(text, extension)

        return DataType.BINARY

    def _classify_text(self, text: str, extension: str) -> DataType:
        stripped = text.lstrip()
        if stripped[:15].lower().startswith("<!doctype html") or stripped[:6].lower().startswith("<html"):
            return DataType.DOCUMENT
        if extension in _DOCUMENT_TEXT_EXTENSIONS:
            return DataType.DOCUMENT

        json_type = _classify_json(text)
        if json_type is not None:
            return json_type

        delimited_type = _classify_delimited(text)
        if delimited_type is not None:
            return delimited_type

        if _looks_like_log(text):
            return DataType.LOG

        return DataType.TEXT


def classify_and_profile(content: bytes, filename: str | None = None) -> DataProfile:
    data_type = DataClassifier().classify(content, filename)
    dimensions = None
    if data_type is DataType.TENSOR:
        npy = detector.detect_numpy_tensor(content)
        if npy is not None:
            dimensions = npy[1]
    return build_profile(data_type, content, dimensions=dimensions)


def _try_decode(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _classify_json(text: str) -> DataType | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, list) and parsed and all(isinstance(row, dict) for row in parsed):
        return DataType.TABULAR
    if isinstance(parsed, dict):
        return DataType.DATABASE_RECORD
    return None


def _classify_delimited(text: str) -> DataType | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    for delimiter in (",", "\t"):
        sample = lines[:20]
        counts = [line.count(delimiter) for line in sample]
        if counts[0] > 0 and len(set(counts)) == 1:
            header = lines[0].lower()
            if any(hint in header for hint in ("timestamp", "date", "time")):
                return DataType.TIME_SERIES
            return DataType.TABULAR
    return None


def _looks_like_log(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()][:50]
    if len(lines) < 3:
        return False
    matches = sum(1 for line in lines if _LOG_LINE_PATTERN.match(line))
    return (matches / len(lines)) >= 0.6
