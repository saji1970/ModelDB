"""Content-based signature detection (CLAUDE-STORAGE.md section 6).

"file = model.bin must not automatically mean AI_MODEL. Inspect the
content where practical." Every check here inspects actual bytes -
magic numbers, container structure, header parsing - not just a
filename extension. `classifier.py` falls back to extension only for
the handful of formats (ONNX's protobuf, in particular) where a cheap,
reliable content signature doesn't exist; that's a documented, honest
gap, not a shortcut applied everywhere.
"""

from __future__ import annotations

import json
import struct
import zipfile
from dataclasses import dataclass
from io import BytesIO


@dataclass(frozen=True)
class Signature:
    data_type: str  # DataType value, kept as str to avoid a circular import
    detail: str


def _starts_with(content: bytes, *prefixes: bytes) -> bool:
    return any(content.startswith(p) for p in prefixes)


def detect_image(content: bytes) -> Signature | None:
    if _starts_with(content, b"\xff\xd8\xff"):
        return Signature("IMAGE", "jpeg")
    if _starts_with(content, b"\x89PNG\r\n\x1a\n"):
        return Signature("IMAGE", "png")
    if _starts_with(content, b"GIF87a", b"GIF89a"):
        return Signature("IMAGE", "gif")
    if _starts_with(content, b"BM"):
        return Signature("IMAGE", "bmp")
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WEBP":
        return Signature("IMAGE", "webp")
    if _starts_with(content, b"II*\x00", b"MM\x00*"):
        return Signature("IMAGE", "tiff")
    return None


def detect_audio(content: bytes) -> Signature | None:
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WAVE":
        return Signature("AUDIO", "wav")
    if _starts_with(content, b"fLaC"):
        return Signature("AUDIO", "flac")
    if _starts_with(content, b"ID3") or _starts_with(content, b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return Signature("AUDIO", "mp3")
    if _starts_with(content, b"OggS"):
        return Signature("AUDIO", "ogg")
    return None


def detect_video(content: bytes) -> Signature | None:
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return Signature("VIDEO", "mp4/mov")
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:11] == b"AVI":
        return Signature("VIDEO", "avi")
    if _starts_with(content, b"\x1a\x45\xdf\xa3"):
        return Signature("VIDEO", "mkv/webm")
    return None


def detect_document(content: bytes) -> Signature | None:
    if _starts_with(content, b"%PDF-"):
        return Signature("DOCUMENT", "pdf")
    return None


def detect_archive_container(content: bytes) -> Signature | None:
    """ZIP/GZIP/TAR - a container, not necessarily what's *inside* it.
    `detect_zip_payload` inspects a ZIP's entries to tell an office
    document / AI model checkpoint / plain archive apart."""
    if _starts_with(content, b"PK\x03\x04", b"PK\x05\x06"):
        return Signature("ARCHIVE", "zip")
    if _starts_with(content, b"\x1f\x8b"):
        return Signature("ARCHIVE", "gzip")
    if len(content) >= 262 and content[257:262] == b"ustar":
        return Signature("ARCHIVE", "tar")
    return None


def detect_zip_payload(content: bytes) -> Signature | None:
    """Look inside a ZIP container to distinguish a torch checkpoint
    (pickle-based .pt/.pth/.bin, saved as a ZIP since PyTorch 1.6) from
    an office document (docx/xlsx/pptx, also ZIP-based) from a plain
    archive - exactly the "don't trust the extension" case in section 6."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None

    if any(n == "[Content_Types].xml" for n in names):
        return Signature("DOCUMENT", "office-zip")
    if any(n.endswith("data.pkl") or n.endswith(".pkl") for n in names):
        return Signature("AI_MODEL", "torch-checkpoint-zip")
    return None


def detect_numpy_tensor(content: bytes) -> tuple[Signature, list[int] | None] | None:
    """Real .npy header parsing (not just the magic bytes) so a genuine
    shape can be attached to the DataProfile."""
    if not _starts_with(content, b"\x93NUMPY"):
        return None
    try:
        major = content[6]
        if major == 1:
            header_len = struct.unpack("<H", content[8:10])[0]
            header_start = 10
        else:
            header_len = struct.unpack("<I", content[8:12])[0]
            header_start = 12
        header_text = content[header_start : header_start + header_len].decode("latin1")
        shape = None
        marker = "'shape':"
        if marker in header_text:
            after = header_text.split(marker, 1)[1].lstrip()
            if after.startswith("("):
                inside = after[1 : after.index(")")]
                shape = [int(x) for x in inside.replace(" ", "").rstrip(",").split(",") if x]
        return Signature("TENSOR", "npy"), shape
    except (IndexError, struct.error, ValueError, UnicodeDecodeError):
        return Signature("TENSOR", "npy"), None


def detect_gguf(content: bytes) -> Signature | None:
    if _starts_with(content, b"GGUF"):
        return Signature("AI_MODEL", "gguf")
    return None


def detect_safetensors(content: bytes) -> Signature | None:
    """Real safetensors header parsing: an 8-byte little-endian header
    length, then that many bytes of JSON describing each tensor's
    dtype/shape/offsets - not just a `.safetensors` filename guess."""
    if len(content) < 8:
        return None
    try:
        header_len = struct.unpack("<Q", content[:8])[0]
        if header_len <= 0 or 8 + header_len > len(content):
            return None
        header = json.loads(content[8 : 8 + header_len])
    except (struct.error, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    tensor_entries = [v for k, v in header.items() if k != "__metadata__"]
    if tensor_entries and all(
        isinstance(v, dict) and "dtype" in v and "shape" in v for v in tensor_entries
    ):
        return Signature("AI_MODEL", "safetensors")
    return None


def detect_parquet(content: bytes) -> Signature | None:
    if len(content) >= 8 and content[:4] == b"PAR1" and content[-4:] == b"PAR1":
        return Signature("TABULAR", "parquet")
    return None
