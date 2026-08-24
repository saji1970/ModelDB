"""dna/crypto.py: the encryption layer applied before DNA encoding."""

import pytest

from mdc.dna.crypto import DecryptionError, decrypt, encrypt

KEY = bytes(range(32))


def test_encrypt_then_decrypt_round_trips():
    plaintext = b"a payload the DNA tier will encode as ACGT"
    blob = encrypt(KEY, plaintext)
    assert decrypt(KEY, blob) == plaintext


def test_ciphertext_does_not_contain_the_plaintext():
    plaintext = b"the quick brown fox jumps over the lazy dog"
    blob = encrypt(KEY, plaintext)
    assert plaintext not in blob


def test_two_encryptions_of_the_same_plaintext_differ():
    plaintext = b"same input"
    assert encrypt(KEY, plaintext) != encrypt(KEY, plaintext)  # random nonce each call


def test_wrong_key_fails_to_decrypt():
    blob = encrypt(KEY, b"secret")
    wrong_key = bytes([9] * 32)
    with pytest.raises(DecryptionError):
        decrypt(wrong_key, blob)


def test_tampered_ciphertext_is_rejected():
    blob = bytearray(encrypt(KEY, b"original value"))
    blob[-1] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt(KEY, bytes(blob))


def test_truncated_blob_is_rejected():
    with pytest.raises(DecryptionError):
        decrypt(KEY, b"short")


def test_empty_plaintext_round_trips():
    blob = encrypt(KEY, b"")
    assert decrypt(KEY, blob) == b""
