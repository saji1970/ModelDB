"""Document format detection, text extraction, and chunking
(CLAUDE-STORAGE.md section 20).

Text extraction only ever returns text it actually read from the
bytes - PDF and office documents (.docx/.xlsx/.pptx) have no
dependency-free parser available here, so `extract_text` honestly
returns `None` for them rather than fabricating placeholder text. This
mirrors the same documented gap as ONNX classification (Phase A) and
non-safetensors model formats (Phase D): a real capability with a
stated boundary, not a silent guess past that boundary.
"""

from __future__ import annotations

import html as html_module
import re
from pathlib import Path

from mdc.classification import detector

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_LINE_RE = re.compile(r"\n\s*\n")

DEFAULT_CHUNK_CHAR_LIMIT = 1000


def detect_document_format(content: bytes, filename: str | None = None) -> str:
    if content.startswith(b"%PDF-"):
        return "pdf"

    archive = detector.detect_archive_container(content)
    if archive is not None and archive.detail == "zip":
        payload = detector.detect_zip_payload(content)
        if payload is not None and payload.detail == "office-zip":
            return "office-zip"

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"

    stripped = text.lstrip()
    if stripped[:15].lower().startswith("<!doctype html") or stripped[:6].lower().startswith("<html"):
        return "html"

    extension = Path(filename).suffix.lower() if filename else ""
    if extension in (".md", ".markdown"):
        return "markdown"
    if extension in (".html", ".htm"):
        return "html"
    return "text"


def _strip_html(markup: str) -> str:
    without_tags = _TAG_RE.sub(" ", markup)
    unescaped = html_module.unescape(without_tags)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


def extract_text(content: bytes, document_format: str) -> str | None:
    if document_format not in ("text", "markdown", "html"):
        return None  # pdf/office-zip/unknown - no parser available, not guessed
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _strip_html(decoded) if document_format == "html" else decoded


def chunk_text(text: str, max_chars: int = DEFAULT_CHUNK_CHAR_LIMIT) -> list[str]:
    if not text or not text.strip():
        return []

    paragraphs = [p.strip() for p in _BLANK_LINE_RE.split(text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
        else:
            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start : start + max_chars])
            current = ""
    if current:
        chunks.append(current)
    return chunks
