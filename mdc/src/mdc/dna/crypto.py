"""Encrypts payload bytes before they reach the DNA encoder.

`dna/encoder.py`'s 2-bit-per-base mapping is a fixed, public scheme -
encoding *plaintext* into ACGT would only be an obfuscation, reversible
by anyone who has read this file (or CLAUDE.md section 34). Encrypting
first and DNA-encoding the ciphertext means the on-disk/in-memory
sequence is unreadable without the key this project holds, matching
the same "unreadable outside our own API/CLI" property mdc-lite
already guarantees for its own store.

AES-256-GCM via the `cryptography` package - a 12-byte random nonce
per call, prepended to the ciphertext, mirroring the nonce-then-
ciphertext layout `mdc-lite/src/lib.rs` uses for its own entries.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12


class DecryptionError(ValueError):
    """Wrong key, or the bytes were corrupted/tampered with - AES-GCM's
    authentication tag doesn't distinguish the two, and telling an
    attacker which one happened would itself be information leakage."""


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def decrypt(key: bytes, blob: bytes) -> bytes:
    if len(blob) < NONCE_LEN:
        raise DecryptionError("ciphertext too short to contain a nonce")
    nonce, ciphertext = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # cryptography raises InvalidTag, kept generic per the docstring above
        raise DecryptionError("decryption failed (wrong key or corrupted/tampered data)") from exc
