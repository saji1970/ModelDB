"""Phase E: Image + Document storage (CLAUDE-STORAGE.md sections 19-20)."""

import struct
from pathlib import Path

import pytest

from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.objects.document import DocumentStore
from mdc.objects.document_text import chunk_text, detect_document_format, extract_text
from mdc.objects.errors import DocumentNotFoundError, ImageNotFoundError
from mdc.objects.image import ImageStore, detect_image_format
from mdc.objects.image_dimensions import extract_image_dimensions
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import CompressionAlgorithm, StorageStrategyEngine


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


@pytest.fixture
def image_store(store: DuckDBStore) -> ImageStore:
    return ImageStore(build_default_router(store))


@pytest.fixture
def document_store(store: DuckDBStore) -> DocumentStore:
    return DocumentStore(build_default_router(store))


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 50


def _jpeg(width: int, height: int) -> bytes:
    sof_payload = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 2 + len(sof_payload)) + sof_payload + b"\xff\xd9"


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 10


def _bmp(width: int, height: int) -> bytes:
    return b"BM" + b"\x00" * 16 + struct.pack("<ii", width, height) + b"\x00" * 20


# -- image dimension parsing (real header parsing, no library) -------------------

def test_detect_image_format_from_content_signature():
    assert detect_image_format(_png(1, 1)) == "png"
    assert detect_image_format(_jpeg(1, 1)) == "jpeg"
    assert detect_image_format(_gif(1, 1)) == "gif"
    assert detect_image_format(b"not an image") is None


def test_png_dimensions_parsed_from_ihdr_chunk():
    assert extract_image_dimensions(_png(800, 600), "png") is not None
    dims = extract_image_dimensions(_png(800, 600), "png")
    assert (dims.width, dims.height) == (800, 600)


def test_jpeg_dimensions_parsed_from_sof0_marker():
    dims = extract_image_dimensions(_jpeg(640, 480), "jpeg")
    assert (dims.width, dims.height) == (640, 480)


def test_gif_dimensions_parsed_from_logical_screen_descriptor():
    dims = extract_image_dimensions(_gif(320, 240), "gif")
    assert (dims.width, dims.height) == (320, 240)


def test_bmp_dimensions_parsed_and_negative_height_is_normalized():
    dims = extract_image_dimensions(_bmp(100, -200), "bmp")  # top-down bitmap
    assert (dims.width, dims.height) == (100, 200)


def test_unparseable_format_returns_none_not_a_guess():
    assert extract_image_dimensions(b"RIFF....WEBP" + b"\x00" * 20, "webp") is None


# -- ImageStore ---------------------------------------------------------------------

def test_store_and_retrieve_image_round_trips_exact_bytes(image_store: ImageStore):
    png = _png(1920, 1080)
    metadata = image_store.store_image(png)
    assert image_store.retrieve_image(metadata.image_id) == png


def test_image_metadata_captures_real_dimensions_and_format(image_store: ImageStore):
    metadata = image_store.store_image(_jpeg(1024, 768))
    assert metadata.format == "jpeg"
    assert metadata.width == 1024
    assert metadata.height == 768
    assert metadata.size_bytes == len(_jpeg(1024, 768))


def test_get_metadata_does_not_require_fetching_original_bytes(image_store: ImageStore):
    metadata = image_store.store_image(_png(64, 64))
    fetched = image_store.get_metadata(metadata.image_id)
    assert fetched == metadata


def test_unknown_image_id_raises(image_store: ImageStore):
    with pytest.raises(ImageNotFoundError):
        image_store.retrieve_image("nope")
    with pytest.raises(ImageNotFoundError):
        image_store.get_metadata("nope")


def test_already_compressed_high_entropy_image_is_not_recompressed(store: DuckDBStore):
    # High-entropy payload, like a real compressed photo - compression
    # must not be selected "blindly" (section 19). This isn't
    # image-specific logic; it falls out of Phase B's entropy-based
    # `select_compression` for free.
    high_entropy_jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 20
    profile = build_profile(DataType.IMAGE, high_entropy_jpeg)
    strategy = StorageStrategyEngine().select(profile)
    assert strategy.compression is CompressionAlgorithm.NONE

    image_store = ImageStore(build_default_router(store))
    metadata = image_store.store_image(high_entropy_jpeg)
    assert image_store.retrieve_image(metadata.image_id) == high_entropy_jpeg


# -- document format detection + text extraction -----------------------------------

def test_detect_document_format_pdf_html_markdown_text():
    assert detect_document_format(b"%PDF-1.7\n...") == "pdf"
    assert detect_document_format(b"<!DOCTYPE html><html></html>") == "html"
    assert detect_document_format(b"# Title", "notes.md") == "markdown"
    assert detect_document_format(b"plain words", "notes.txt") == "text"


def test_extract_text_strips_html_tags_and_unescapes_entities():
    html = b"<html><body><p>Rock &amp; Roll</p><p>Second</p></body></html>"
    text = extract_text(html, "html")
    assert "<p>" not in text
    assert "Rock & Roll" in text


def test_extract_text_returns_none_for_pdf_and_office_zip():
    assert extract_text(b"%PDF-1.7\njunk", "pdf") is None
    assert extract_text(b"whatever", "office-zip") is None


def test_chunk_text_splits_on_paragraph_boundaries_within_limit():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = chunk_text(text, max_chars=1000)
    assert len(chunks) == 1  # fits in one chunk together
    assert chunks[0] == text


def test_chunk_text_splits_when_exceeding_the_limit():
    paragraphs = [f"Paragraph number {i} with some words in it." for i in range(20)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_chunk_text_hard_splits_a_single_oversized_paragraph():
    huge_paragraph = "word " * 500  # no blank lines at all
    chunks = chunk_text(huge_paragraph, max_chars=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)


def test_chunk_text_of_empty_string_is_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


# -- DocumentStore --------------------------------------------------------------------

def test_store_and_retrieve_original_round_trips_exact_bytes(document_store: DocumentStore):
    content = b"# Title\n\nSome markdown body text here."
    record = document_store.store_document(content, filename="notes.md")
    assert document_store.retrieve_original(record.document_id) == content


def test_document_record_has_text_and_chunks_for_markdown(document_store: DocumentStore):
    content = b"# Title\n\nFirst paragraph.\n\nSecond paragraph."
    record = document_store.store_document(content, filename="notes.md")
    assert record.text is not None
    assert record.chunks
    assert record.metadata.format == "markdown"
    assert record.metadata.text_length == len(content.decode())


def test_document_record_has_no_text_for_pdf(document_store: DocumentStore):
    content = b"%PDF-1.7\n" + b"binary junk" * 20
    record = document_store.store_document(content, filename="report.pdf")
    assert record.metadata.format == "pdf"
    assert record.text is None
    assert record.chunks == []


def test_get_record_round_trips(document_store: DocumentStore):
    content = b"plain text document"
    record = document_store.store_document(content, filename="a.txt")
    assert document_store.get_record(record.document_id) == record


def test_original_and_record_are_independently_retrievable(document_store: DocumentStore):
    # Retrieving the record must not require touching the original bytes
    # object, and vice versa (section 20: independent original/derived data).
    content = b"# Doc\n\nBody."
    record = document_store.store_document(content, filename="a.md")
    original = document_store.retrieve_original(record.document_id)
    fetched_record = document_store.get_record(record.document_id)
    assert original == content
    assert fetched_record.text is not None


def test_unknown_document_id_raises(document_store: DocumentStore):
    with pytest.raises(DocumentNotFoundError):
        document_store.retrieve_original("nope")
    with pytest.raises(DocumentNotFoundError):
        document_store.get_record("nope")
