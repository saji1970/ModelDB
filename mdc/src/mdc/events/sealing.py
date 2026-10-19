"""Sealing: AES-256-GCM, one key per stream.

The stream's key is derived (HKDF-SHA256) from the server's master key and
the stream id, so reading one stream's key out of memory never yields
another's, and these keys are independent of the DNA tier's use of the
same master key (different HKDF `info`).

Each record is bound to its place by the AEAD "associated data"
(stream id, kind, sequence number): a record copied into another stream,
moved to another position, or re-labelled as a different kind fails
authentication when read. A deleted middle event shows up as a gap in the
sequence, which the store refuses to replay across.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mdc.events.errors import StreamCorruptError

_NONCE_LEN = 12


def derive_stream_key(master_key: bytes, stream_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"mdc-events/v1/" + stream_id.encode("utf-8")).derive(master_key)


def _aad(stream_id: str, kind: str, seq: int) -> bytes:
    return f"{stream_id}|{kind}|{seq}".encode("utf-8")


def seal(key: bytes, stream_id: str, kind: str, seq: int, plaintext: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(stream_id, kind, seq))


def open_sealed(key: bytes, stream_id: str, kind: str, seq: int, blob: bytes) -> bytes:
    if len(blob) < _NONCE_LEN + 16:
        raise StreamCorruptError(stream_id, f"{kind} record {seq} is truncated")
    try:
        return AESGCM(key).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], _aad(stream_id, kind, seq))
    except InvalidTag:
        raise StreamCorruptError(stream_id, f"{kind} record {seq} was altered, moved, or sealed with a different key") from None
