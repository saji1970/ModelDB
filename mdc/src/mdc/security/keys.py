"""At-rest encryption key for the DNA storage tier (CLAUDE.md section
49: AuthProvider covers request authorization; this covers the
separate concern of protecting bytes once they're written).

`MDC_DNA_KEY` (64 hex chars = 32 bytes) is the deployment-time answer,
matching mdc-lite's model of "the caller supplies the key, this
project never has an opinion about its custody." Without it, a key is
generated once and persisted to `~/.mdc/dna.key` (mode 0600) so the
archive tier stays readable across restarts - the honest tradeoff is
that the key then lives on the same host as the data, same as any
default-enabled disk encryption. This is what makes DNA-tier bytes
unreadable to anything that isn't holding this key, i.e. this
project's own API/CLI - not a claim that the key itself is
hardware-protected the way mdc-lite defers to Secure Enclave/Keystore.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

KEY_LEN = 32
_ENV_VAR = "MDC_DNA_KEY"
_DEFAULT_KEY_PATH = Path.home() / ".mdc" / "dna.key"


def load_or_create_dna_key(path: Path | None = None) -> bytes:
    env_value = os.environ.get(_ENV_VAR)
    if env_value:
        try:
            key = bytes.fromhex(env_value.strip())
        except ValueError as exc:
            raise ValueError(f"{_ENV_VAR} must be {KEY_LEN * 2} hex characters") from exc
        if len(key) != KEY_LEN:
            raise ValueError(f"{_ENV_VAR} must decode to exactly {KEY_LEN} bytes ({KEY_LEN * 2} hex chars), got {len(key)}")
        return key

    key_path = path or _DEFAULT_KEY_PATH
    if key_path.exists():
        key = bytes.fromhex(key_path.read_text().strip())
        if len(key) != KEY_LEN:
            raise ValueError(f"{key_path} does not contain a valid {KEY_LEN}-byte key")
        return key

    key = secrets.token_bytes(KEY_LEN)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key.hex())
    key_path.chmod(0o600)
    return key
