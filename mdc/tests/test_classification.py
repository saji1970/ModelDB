"""Phase A: DataType classification + DataProfile + MDCObject
(CLAUDE-STORAGE.md sections 4-8, 33, required tests in section 48).
"""

import json
import struct
import zipfile
from io import BytesIO

from mdc.classification.classifier import DataClassifier, classify_and_profile
from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile, shannon_entropy
from mdc.model.object import MDCObject, generate_object_id, utcnow

classifier = DataClassifier()


def _safetensors_bytes() -> bytes:
    header = {
        "weight": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]},
        "__metadata__": {"format": "pt"},
    }
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"\x00" * 16


def _npy_bytes(shape: tuple[int, ...] = (2, 3)) -> bytes:
    header = f"{{'descr': '<f8', 'fortran_order': False, 'shape': {shape}, }}"
    padding = (64 - (10 + len(header) + 1) % 64) % 64
    header = header + " " * padding + "\n"
    return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1") + b"\x00" * 48


def _torch_checkpoint_zip_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("archive/data.pkl", b"\x80\x02}q\x00.")
        zf.writestr("archive/data/0", b"\x00" * 8)
    return buffer.getvalue()


def _docx_like_zip_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


# -- section 48 required classification tests -----------------------------------

def test_detect_model():
    assert classifier.classify(_safetensors_bytes(), "model.safetensors") is DataType.AI_MODEL
    assert classifier.classify(b"GGUF" + b"\x00" * 32, "model.gguf") is DataType.AI_MODEL
    assert classifier.classify(_torch_checkpoint_zip_bytes(), "checkpoint.pt") is DataType.AI_MODEL


def test_detect_tensor():
    assert classifier.classify(_npy_bytes(), "weights.npy") is DataType.TENSOR


def test_detect_image():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    assert classifier.classify(png, "photo.png") is DataType.IMAGE
    assert classifier.classify(jpeg, "photo.jpg") is DataType.IMAGE


def test_detect_document():
    pdf = b"%PDF-1.7\n" + b"\x00" * 32
    assert classifier.classify(pdf, "report.pdf") is DataType.DOCUMENT
    assert classifier.classify(_docx_like_zip_bytes(), "report.docx") is DataType.DOCUMENT
    assert classifier.classify(b"# Title\n\nSome markdown body.", "notes.md") is DataType.DOCUMENT


def test_detect_video():
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
    assert classifier.classify(mp4, "clip.mp4") is DataType.VIDEO


def test_detect_audio():
    wav = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 16
    assert classifier.classify(wav, "sound.wav") is DataType.AUDIO


def test_detect_tabular():
    csv_bytes = b"merchant_id,name,country\nM1,ABC Store,IN\nM2,XYZ Retail,US\n"
    assert classifier.classify(csv_bytes, "merchants.csv") is DataType.TABULAR

    json_records = json.dumps([{"merchant_id": "M1"}, {"merchant_id": "M2"}]).encode("utf-8")
    assert classifier.classify(json_records, "merchants.json") is DataType.TABULAR


# -- content over filename (section 6's core rule) -------------------------------

def test_bin_extension_alone_is_not_assumed_to_be_a_model():
    plain_bytes = b"just some arbitrary bytes, not a real model file \x01\x02\x03"
    assert classifier.classify(plain_bytes, "model.bin") is not DataType.AI_MODEL


def test_zip_content_decides_between_docx_and_torch_checkpoint():
    # Same container format (ZIP); the classification differs by what's inside.
    assert classifier.classify(_docx_like_zip_bytes(), "output.dat") is DataType.DOCUMENT
    assert classifier.classify(_torch_checkpoint_zip_bytes(), "output.dat") is DataType.AI_MODEL


# -- other data types --------------------------------------------------------------

def test_detect_database_record():
    record = json.dumps({"merchant_id": "M1", "name": "ABC Store", "balance": 15000}).encode("utf-8")
    assert classifier.classify(record, "record.json") is DataType.DATABASE_RECORD


def test_detect_time_series_from_timestamp_column():
    csv_bytes = b"timestamp,value\n2026-01-01T00:00:00,10\n2026-01-01T00:01:00,12\n"
    assert classifier.classify(csv_bytes, "metrics.csv") is DataType.TIME_SERIES


def test_detect_log_from_timestamped_lines():
    log_bytes = (
        b"2026-01-01 00:00:01 INFO starting up\n"
        b"2026-01-01 00:00:02 INFO ready\n"
        b"2026-01-01 00:00:03 ERROR something broke\n"
    )
    assert classifier.classify(log_bytes, "app.log") is DataType.LOG


def test_unknown_for_empty_content():
    assert classifier.classify(b"", "empty.dat") is DataType.UNKNOWN


def test_binary_fallback_for_undecodable_unrecognized_content():
    assert classifier.classify(bytes(range(256)), "mystery.dat") is DataType.BINARY


# -- DataProfile --------------------------------------------------------------------

def test_shannon_entropy_of_uniform_zero_bytes_is_low():
    assert shannon_entropy(b"\x00" * 1000) < 0.1


def test_shannon_entropy_of_full_byte_range_is_high():
    assert shannon_entropy(bytes(range(256)) * 10) > 0.9


def test_build_profile_flags_tensor_and_matrix_candidate():
    profile = build_profile(DataType.TENSOR, _npy_bytes(), dimensions=[2, 3])
    assert profile.matrix_candidate is True
    assert profile.tensor_candidate is True
    assert profile.dimensions == [2, 3]
    assert profile.structured is True


def test_build_profile_marks_database_record_as_mutable_and_unstructured_matrix():
    profile = build_profile(DataType.DATABASE_RECORD, b'{"a": 1}')
    assert profile.mutable is True
    assert profile.matrix_candidate is False


def test_classify_and_profile_end_to_end():
    profile = classify_and_profile(_npy_bytes(shape=(4, 4)), "weights.npy")
    assert profile.data_type is DataType.TENSOR
    assert profile.dimensions == [4, 4]
    assert profile.size_bytes == len(_npy_bytes(shape=(4, 4)))


# -- MDCObject ------------------------------------------------------------------

def test_mdc_object_round_trips_through_json():
    now = utcnow()
    obj = MDCObject(
        object_id=generate_object_id(DataType.IMAGE),
        object_type=DataType.IMAGE,
        size=2048,
        created_at=now,
        updated_at=now,
        metadata={"filename": "photo.png"},
    )
    restored = MDCObject.model_validate_json(obj.model_dump_json())
    assert restored.object_type is DataType.IMAGE
    assert restored.metadata["filename"] == "photo.png"


def test_generate_object_id_reflects_type_prefix():
    object_id = generate_object_id(DataType.AI_MODEL)
    assert object_id.startswith("AIM-")
