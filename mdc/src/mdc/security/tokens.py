"""API bearer-token issuance and verification (CLAUDE.md section 49:
AuthProvider). This is what "custom apps connecting to it should use
tokenisation for secure access" actually means in this codebase: CORS
is wide open (see `api/app.py`'s module docstring) precisely so a
third-party NLU/custom UI can call this API cross-origin, and a token
is the real access control that was missing - a browser same-origin
policy was never a security boundary against a non-browser client
anyway.

Tokens are opaque random strings (`mdc_<32 random bytes, url-safe>`);
only their SHA-256 hash is ever persisted, the same reasoning as a
password hash - a leaked token-store file doesn't hand out working
credentials. The plaintext token exists only once, in `issue()`'s
return value, at the moment it's created.

Every token carries a `Role` (`security/roles.py`) - `issue()` defaults
to `Role.ADMIN` so a bare `mdc token issue <name>` keeps behaving
exactly as it did before roles existed (any valid token = full access);
callers who want a restricted integration now pass `--role viewer` (or
`editor`/`db_admin`) explicitly instead. A token predating this field
(no `role` key in the JSON) is loaded as `Role.VIEWER`, the safe
default for a record nobody can regenerate on this specific point.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from mdc.security.roles import Role

_ENV_TOKENS = "MDC_API_TOKENS"  # comma-separated - for CI/deployment, bypasses the on-disk store entirely
_DEFAULT_STORE_PATH = Path.home() / ".mdc" / "api_tokens.json"
_TOKEN_PREFIX = "mdc_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class TokenRecord:
    name: str
    token_hash: str
    role: Role = Role.ADMIN


class TokenStore:
    """Persisted at `path` (default `~/.mdc/api_tokens.json`, mode 0600)
    as a list of `{name, token_hash}` records - never a plaintext
    token."""

    def __init__(self, path: Path | None = None):
        self.path = path or _DEFAULT_STORE_PATH
        self._records: list[TokenRecord] = self._load()

    def _load(self) -> list[TokenRecord]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        # A record with no "role" key predates roles entirely - loaded as
        # VIEWER (least privilege) rather than silently inheriting the
        # new ADMIN default meant only for freshly-issued tokens.
        return [
            TokenRecord(name=r["name"], token_hash=r["token_hash"], role=Role(r["role"]) if "role" in r else Role.VIEWER)
            for r in data
        ]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            [{"name": r.name, "token_hash": r.token_hash, "role": r.role.value} for r in self._records], indent=2,
        ))
        self.path.chmod(0o600)

    def issue(self, name: str, role: Role = Role.ADMIN) -> str:
        """Creates a new token under `name` and returns its plaintext -
        the only time it's ever available. Names aren't unique keys;
        issuing again under the same name adds a second, independent
        token rather than replacing the first (use `revoke` for that).
        `role` defaults to ADMIN so existing callers/tests keep working
        unchanged; pass an explicit lesser role for a restricted
        integration."""
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._records.append(TokenRecord(name=name, token_hash=_hash(token), role=role))
        self._save()
        return token

    def revoke(self, name: str) -> int:
        """Removes every token issued under `name`, returning how many
        were removed."""
        before = len(self._records)
        self._records = [r for r in self._records if r.name != name]
        removed = before - len(self._records)
        if removed:
            self._save()
        return removed

    def list_names(self) -> list[str]:
        return [r.name for r in self._records]

    def verify(self, token: str) -> bool:
        """True if `token` is currently valid, regardless of role."""
        return self.resolve(token) is not None

    def resolve(self, token: str) -> TokenRecord | None:
        """Returns the matching `TokenRecord` (so callers can check its
        role) or `None` if the token is missing/unknown/revoked.
        Re-reads the store from disk on every call, deliberately not
        relying on `self._records` - a long-running `mdc serve` process
        must recognize a token issued by a separate, later `mdc token
        issue` invocation without needing a restart."""
        if not token:
            return None
        env_tokens = os.environ.get(_ENV_TOKENS)
        if env_tokens and token in {t.strip() for t in env_tokens.split(",") if t.strip()}:
            # Operator-configured deployment/CI credential, not something
            # an end-user token-holder gets by default - treated as fully
            # trusted, the same as whoever set the env var already is.
            return TokenRecord(name="env", token_hash=_hash(token), role=Role.ADMIN)
        digest = _hash(token)
        return next((r for r in self._load() if r.token_hash == digest), None)
