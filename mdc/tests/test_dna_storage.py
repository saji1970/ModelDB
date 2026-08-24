"""Phase H: DNA storage simulation (CLAUDE-STORAGE.md sections 25-26,
required tests in section 48: test_dna_encode_decode, test_dna_checksum).
"""

import hashlib
import random
from pathlib import Path

import pytest

from mdc.dna.corruption import CorruptionRates, corrupt_sequence
from mdc.dna.crypto import DecryptionError
from mdc.dna.ecc import ECCDecodeResult, RepetitionECC
from mdc.dna.encoder import DNADecodeError, decode, encode
from mdc.dna.storage import DNAStorageBackend
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import StorageTier


# -- section 48 required tests: encode/decode + checksum ------------------------

@pytest.mark.parametrize("payload", [b"", b"\x00", b"\xff\xff\xff\xff", b"hello molecular world!", bytes(range(256))])
def test_dna_encode_decode(payload: bytes):
    assert decode(encode(payload)) == payload


def test_dna_checksum():
    payload = b"the checksum must reflect the original payload"
    backend = DNAStorageBackend()
    checksum = backend.put("b1", payload)
    assert checksum == hashlib.sha256(payload).hexdigest()
    assert backend.metadata("b1")["checksum"] == checksum


# -- encoder correctness -----------------------------------------------------------

def test_encoded_sequence_uses_only_acgt():
    sequence = encode(bytes(range(256)))
    assert set(sequence) <= set("ACGT")


def test_encoded_sequence_length_is_four_bases_per_byte():
    payload = b"twelve bytes"
    assert len(payload) == 12
    assert len(encode(payload)) == 48


def test_decode_rejects_length_not_a_multiple_of_four():
    with pytest.raises(DNADecodeError):
        decode("ACG")  # 3 bases


def test_decode_rejects_non_acgt_symbols():
    with pytest.raises(DNADecodeError):
        decode("ACGX")


# -- corruption simulator -----------------------------------------------------------

def test_zero_rates_is_a_true_noop():
    sequence = encode(b"unchanged payload")
    rng = random.Random(1)
    assert corrupt_sequence(sequence, CorruptionRates(), rng) == sequence


def test_full_dropout_always_drops_the_sequence():
    sequence = encode(b"gone")
    rng = random.Random(1)
    assert corrupt_sequence(sequence, CorruptionRates(dropout_rate=1.0), rng) is None


def test_full_deletion_empties_the_sequence():
    sequence = encode(b"gone")
    rng = random.Random(1)
    result = corrupt_sequence(sequence, CorruptionRates(deletion_rate=1.0), rng)
    assert result == ""


def test_full_substitution_changes_every_base_but_keeps_length():
    sequence = encode(b"substitute me")
    rng = random.Random(1)
    result = corrupt_sequence(sequence, CorruptionRates(substitution_rate=1.0), rng)
    assert len(result) == len(sequence)
    assert all(a != b for a, b in zip(sequence, result))


def test_corruption_is_deterministic_given_the_same_seed():
    sequence = encode(b"deterministic please")
    rates = CorruptionRates(substitution_rate=0.2, insertion_rate=0.1, deletion_rate=0.1)
    a = corrupt_sequence(sequence, rates, random.Random(99))
    b = corrupt_sequence(sequence, rates, random.Random(99))
    assert a == b


def test_invalid_rate_raises():
    with pytest.raises(ValueError):
        CorruptionRates(substitution_rate=1.5)


# -- RepetitionECC: fully controlled, deterministic corruption -------------------
# (Not relying on the random simulator here - hand-corrupting specific byte
# positions gives a test that proves the majority-vote *guarantee* itself,
# rather than depending on a seed happening to produce a favorable outcome.)

def test_ecc_recovers_when_each_copy_is_corrupted_at_a_different_position():
    original = bytearray(b"the quick brown fox jumps over the lazy dog")
    copy_a, copy_b, copy_c = bytearray(original), bytearray(original), bytearray(original)
    copy_a[5] ^= 0xFF
    copy_b[20] ^= 0xFF
    # copy_c untouched

    result = RepetitionECC(copies=3).decode([bytes(copy_a), bytes(copy_b), bytes(copy_c)])
    assert result.data == bytes(original)
    assert result.recovered is True
    assert result.corrected_byte_count == 2


def test_ecc_can_fail_at_a_position_where_two_copies_disagree_differently():
    # A real, honest limitation: majority voting only works when the
    # correct value has a strict plurality. Two DIFFERENT wrong values at
    # the same position (plus one correct) is a 3-way tie the algorithm
    # cannot be expected to resolve correctly - it must not pretend to.
    original = bytearray(b"the quick brown fox jumps over the lazy dog")
    copy_a, copy_b, copy_c = bytearray(original), bytearray(original), bytearray(original)
    copy_a[10] = (copy_a[10] + 1) % 256
    copy_b[10] = (copy_b[10] + 2) % 256

    result = RepetitionECC(copies=3).decode([bytes(copy_a), bytes(copy_b), bytes(copy_c)])
    assert result.data != bytes(original)  # honestly wrong here, not silently accepted as correct


def test_ecc_discards_a_length_mismatched_copy_from_an_indel():
    original = b"fixed length payload here"
    shifted = original[:10] + original[11:]  # one byte shorter, as an indel would cause
    result = RepetitionECC(copies=3).decode([original, original, shifted])
    assert result.data == original  # 2-of-3 same-length copies form the majority
    assert result.usable_copies == 3


def test_ecc_with_missing_copies():
    original = b"payload"
    result = RepetitionECC(copies=3).decode([original, None, None])
    assert result.data == original
    assert result.recovered is False  # a single surviving copy is unverified, not "recovered"
    assert result.usable_copies == 1


def test_ecc_with_no_usable_copies_returns_none():
    result = RepetitionECC(copies=3).decode([None, None, None])
    assert result.data is None
    assert result.usable_copies == 0


def test_ecc_requires_odd_copy_count():
    with pytest.raises(ValueError):
        RepetitionECC(copies=2)


# -- encryption: stored sequences are ciphertext, not plaintext, ACGT -----------

def test_stored_sequence_is_not_the_plaintext_encoding():
    backend = DNAStorageBackend(encryption_key=bytes(range(32)))
    payload = b"anyone reading encoder.py learns the 2-bit mapping - raw ACGT must not be plaintext"
    backend.put("b1", payload)
    stored_sequence = backend.sequences_for("b1")[0]
    assert decode(stored_sequence) != payload
    assert stored_sequence != encode(payload)


def test_wrong_key_cannot_decrypt_a_dna_block():
    writer = DNAStorageBackend(encryption_key=bytes([1] * 32))
    writer.put("b1", b"classified")
    reader = DNAStorageBackend(encryption_key=bytes([2] * 32))
    reader._sequences["b1"] = writer.sequences_for("b1")
    with pytest.raises(DecryptionError):
        reader.get("b1")


def test_default_key_is_persisted_so_a_reopened_backend_can_still_decrypt(tmp_path, monkeypatch):
    monkeypatch.delenv("MDC_DNA_KEY", raising=False)
    monkeypatch.setattr("mdc.security.keys._DEFAULT_KEY_PATH", tmp_path / "dna.key")
    backend_a = DNAStorageBackend()
    backend_a.put("b1", b"still here after reopening")
    backend_b = DNAStorageBackend()  # simulates a fresh process re-reading the same on-disk key
    backend_b._sequences["b1"] = backend_a.sequences_for("b1")
    assert backend_b.get("b1") == b"still here after reopening"


# -- DNAStorageBackend: implements StorageBackend, round-trips losslessly -------

def test_backend_put_get_round_trips_exactly():
    backend = DNAStorageBackend()
    payload = b"stored exactly as given"
    backend.put("b1", payload)
    assert backend.get("b1") == payload


def test_backend_exists_and_delete():
    backend = DNAStorageBackend()
    backend.put("b1", b"data")
    assert backend.exists("b1")
    backend.delete("b1")
    assert not backend.exists("b1")
    with pytest.raises(KeyError):
        backend.get("b1")


def test_backend_search_by_metadata():
    backend = DNAStorageBackend()
    backend.put("b1", b"a", metadata={"kind": "x"})
    backend.put("b2", b"b", metadata={"kind": "y"})
    assert backend.search(kind="x") == ["b1"]
    assert set(backend.search()) == {"b1", "b2"}


def test_backend_with_ecc_copies_still_round_trips_normally():
    # Normal reads never apply corruption - redundancy exists but isn't
    # exercised unless corrupt_and_recover is explicitly called.
    backend = DNAStorageBackend(ecc_copies=3)
    payload = b"redundant but still exact on a normal read"
    backend.put("b1", payload)
    assert backend.get("b1") == payload
    assert len(backend.sequences_for("b1")) == 3


def test_corrupt_and_recover_does_not_mutate_the_stored_block():
    backend = DNAStorageBackend(ecc_copies=3)
    payload = b"the real block must survive the simulation intact"
    backend.put("b1", payload)

    backend.corrupt_and_recover("b1", CorruptionRates(substitution_rate=0.5), seed=1)

    assert backend.get("b1") == payload  # untouched by the simulation


def test_corrupt_and_recover_reports_a_real_result_object():
    backend = DNAStorageBackend(ecc_copies=3)
    backend.put("b1", b"a payload long enough to plausibly take a hit")
    result = backend.corrupt_and_recover("b1", CorruptionRates(dropout_rate=1.0), seed=1)
    assert isinstance(result, ECCDecodeResult)
    assert result.usable_copies == 0  # every copy dropped out
    assert result.data is None


def test_corrupt_and_recover_without_ecc_configured_gives_a_single_copy_result():
    backend = DNAStorageBackend(ecc_copies=1)
    backend.put("b1", b"single copy, no redundancy")
    result = backend.corrupt_and_recover("b1", CorruptionRates(), seed=1)  # zero rates - unchanged
    assert result.data == b"single copy, no redundancy"
    assert result.recovered is False  # nothing to vote against


def test_unknown_block_raises_key_error():
    backend = DNAStorageBackend()
    with pytest.raises(KeyError):
        backend.get("nope")
    with pytest.raises(KeyError):
        backend.corrupt_and_recover("nope", CorruptionRates(), seed=1)


# -- integration: DNAStorageBackend as the router's real ARCHIVE tier -----------

@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


def test_default_router_archive_tier_is_a_real_dna_backend(store: DuckDBStore):
    router = build_default_router(store)
    assert isinstance(router.backends[StorageTier.ARCHIVE], DNAStorageBackend)
    assert router.backends[StorageTier.ARCHIVE] is not router.backends[StorageTier.WARM]


def test_custom_dna_backend_can_be_injected(store: DuckDBStore):
    dna_backend = DNAStorageBackend(ecc_copies=3)
    router = build_default_router(store, dna_backend=dna_backend)
    assert router.backends[StorageTier.ARCHIVE] is dna_backend
