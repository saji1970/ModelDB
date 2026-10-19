"""At-rest encryption key: the DNA storage tier and event-stream sealing
(CLAUDE.md section 49: AuthProvider covers request authorization; this
covers the separate concern of protecting bytes once they're written).

Where the key comes from, first match wins:

1. `MDC_DNA_KEY` - 64 hex chars (32 bytes). The deployment-time answer,
   matching mdc-lite's model of "the caller supplies the key, this
   project never has an opinion about its custody".
2. A key file: the path given by `MDC_DNA_KEY_FILE`, else `~/.mdc/dna.key`.
   If it does not exist a key is generated once and written there (mode
   0600) so the data stays readable across restarts. Point
   `MDC_DNA_KEY_FILE` somewhere that is not on the data volume (a mounted
   secret, another disk) to keep the key apart from the data it protects.

With the default file the key lives on the same host as the data, the
same tradeoff as any default-enabled disk encryption. This makes sealed
bytes unreadable to anything not holding the key - not a claim that the
key is hardware-protected the way mdc-lite defers to Secure Enclave/Keystore.

`key_fingerprint` identifies a key without revealing it, so logs and
`mdc key show` can say *which* key is in use.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_LEN = 32
_ENV_VAR = "MDC_DNA_KEY"
_FILE_ENV_VAR = "MDC_DNA_KEY_FILE"
_DEFAULT_KEY_PATH = Path.home() / ".mdc" / "dna.key"


@dataclass(frozen=True)
class KeySource:
    """Where the key in use comes from: `kind` is "env" or "file"; `path` is
    set for a file; `exists` is False for a file that has not been created yet."""

    kind: str
    path: Path | None
    exists: bool


def generate_key() -> bytes:
    return secrets.token_bytes(KEY_LEN)


def parse_key(text: str, what: str = _ENV_VAR) -> bytes:
    """A 64-hex-character string as a key; ValueError says what is wrong."""
    try:
        key = bytes.fromhex(text.strip())
    except ValueError as exc:
        raise ValueError(f"{what} must be {KEY_LEN * 2} hex characters") from exc
    if len(key) != KEY_LEN:
        raise ValueError(f"{what} must decode to exactly {KEY_LEN} bytes ({KEY_LEN * 2} hex chars), got {len(key)}")
    return key


def key_fingerprint(key: bytes) -> str:
    """8 hex characters that identify `key` without revealing it (a one-way
    derivation, so it cannot be used to recover or test the key)."""
    return HKDF(algorithm=hashes.SHA256(), length=4, salt=None, info=b"mdc-key-fingerprint/v1").derive(key).hex()


def key_source(path: Path | None = None) -> KeySource:
    if os.environ.get(_ENV_VAR):
        return KeySource("env", None, True)
    key_path = path or Path(os.environ.get(_FILE_ENV_VAR) or _DEFAULT_KEY_PATH)
    return KeySource("file", key_path, key_path.exists())


def find_dna_key(path: Path | None = None) -> bytes | None:
    """The key in use, or None if there is none yet (nothing is created)."""
    source = key_source(path)
    if source.kind == "env":
        return parse_key(os.environ[_ENV_VAR])
    if not source.exists:
        return None
    return _read_key_file(source.path)


def load_or_create_dna_key(path: Path | None = None) -> bytes:
    existing = find_dna_key(path)
    if existing is not None:
        return existing
    key_path = key_source(path).path
    key = generate_key()
    write_key_file(key_path, key)
    return key


def write_key_file(path: Path, key: bytes) -> None:
    """Write `key` as hex to `path`, readable only by the owner from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key.hex())
    path.chmod(0o600)


def read_key_file(path: Path) -> bytes:
    return _read_key_file(path)


def _read_key_file(path: Path) -> bytes:
    try:
        key = bytes.fromhex(path.read_text().strip())
    except ValueError:
        raise ValueError(f"{path} does not contain a valid {KEY_LEN}-byte key") from None
    if len(key) != KEY_LEN:
        raise ValueError(f"{path} does not contain a valid {KEY_LEN}-byte key")
    return key


def key_notice() -> str:
    """One line for a startup log: which key will be used and where it lives.
    Never creates a key and never includes the key itself."""
    source = key_source()
    if source.kind == "env":
        return f"Sealing key: from {_ENV_VAR} (fingerprint {key_fingerprint(find_dna_key())})."
    if not source.exists:
        return (
            f"Sealing key: none yet - one will be generated at {source.path} when the first stream is written or object archived. "
            f"Back it up, or set {_ENV_VAR} (or {_FILE_ENV_VAR}) to keep the key apart from the data."
        )
    return (
        f"Sealing key: from file {source.path} (fingerprint {key_fingerprint(find_dna_key())}). "
        f"Set {_ENV_VAR} or point {_FILE_ENV_VAR} off the data volume to keep it apart from the data."
    )
