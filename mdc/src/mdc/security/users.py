"""Interactive user accounts: username + password, for the CLI (`mdc
user ...`) and the first-run admin bootstrap (CLAUDE.md section 49:
AuthProvider). Independent of `security/tokens.py`'s bearer tokens,
which authenticate API callers rather than a person at a terminal -
the two share the same `Role`/`Permission` vocabulary
(`security/roles.py`) but are otherwise separate stores, matching how
this project already keeps the DNA-tier encryption key
(`security/keys.py`) and API tokens as independent concerns.

Passwords are never stored in any recoverable form: `hashlib.scrypt`
(memory-hard, deliberately slow to brute-force, standard-library only)
derives a key from the password and a random per-user salt; only the
salt and derived key are persisted. A leaked `users.json` does not
hand out working passwords, the same reasoning `tokens.py` already
uses for bearer tokens.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from mdc.security.roles import Role

_DEFAULT_STORE_PATH = Path.home() / ".mdc" / "users.json"

# scrypt cost parameters - deliberately expensive (this only runs once
# per login attempt, never in a hot loop) but not so expensive that a
# legitimate `mdc user create`/login on modest hardware feels stuck.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DERIVED_KEY_LEN = 32
_SALT_LEN = 16


class UserAlreadyExistsError(ValueError):
    pass


class UserNotFoundError(ValueError):
    pass


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DERIVED_KEY_LEN,
    )


@dataclass
class User:
    username: str
    role: Role
    salt_hex: str
    derived_key_hex: str

    def check_password(self, password: str) -> bool:
        candidate = _derive(password, bytes.fromhex(self.salt_hex))
        # Constant-time comparison - a password check is exactly the
        # kind of comparison a timing side-channel can matter for.
        return secrets.compare_digest(candidate, bytes.fromhex(self.derived_key_hex))


class UserStore:
    """Persisted at `path` (default `~/.mdc/users.json`, mode 0600) as a
    list of `{username, role, salt, derived_key}` records - never a
    plaintext or reversibly-encrypted password."""

    def __init__(self, path: Path | None = None):
        self.path = path or _DEFAULT_STORE_PATH
        self._users: dict[str, User] = self._load()

    def _load(self) -> dict[str, User]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return {
            r["username"]: User(
                username=r["username"],
                role=Role(r["role"]),
                salt_hex=r["salt"],
                derived_key_hex=r["derived_key"],
            )
            for r in data
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            [
                {"username": u.username, "role": u.role.value, "salt": u.salt_hex, "derived_key": u.derived_key_hex}
                for u in self._users.values()
            ],
            indent=2,
        ))
        self.path.chmod(0o600)

    def is_empty(self) -> bool:
        return len(self._load()) == 0

    def create(self, username: str, password: str, role: Role) -> User:
        self._users = self._load()  # pick up accounts created by another process/instance
        if username in self._users:
            raise UserAlreadyExistsError(f"user {username!r} already exists")
        salt = secrets.token_bytes(_SALT_LEN)
        derived = _derive(password, salt)
        user = User(username=username, role=role, salt_hex=salt.hex(), derived_key_hex=derived.hex())
        self._users[username] = user
        self._save()
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        self._users = self._load()
        user = self._users.get(username)
        if user is None or not user.check_password(password):
            return None
        return user

    def set_role(self, username: str, role: Role) -> None:
        self._users = self._load()
        if username not in self._users:
            raise UserNotFoundError(f"user {username!r} does not exist")
        self._users[username].role = role
        self._save()

    def delete(self, username: str) -> bool:
        self._users = self._load()
        if username not in self._users:
            return False
        del self._users[username]
        self._save()
        return True

    def list_users(self) -> list[User]:
        return sorted(self._load().values(), key=lambda u: u.username)
