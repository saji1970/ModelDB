"""security/users.py: user accounts, password hashing, and roles."""

import pytest

from mdc.security.roles import Role
from mdc.security.users import UserAlreadyExistsError, UserNotFoundError, UserStore


def test_create_then_authenticate(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("alice", "correct horse", Role.EDITOR)
    user = store.authenticate("alice", "correct horse")
    assert user is not None
    assert user.username == "alice"
    assert user.role is Role.EDITOR


def test_authenticate_rejects_wrong_password(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("alice", "correct horse", Role.VIEWER)
    assert store.authenticate("alice", "wrong password") is None


def test_authenticate_rejects_unknown_username(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.authenticate("nobody", "whatever") is None


def test_duplicate_username_raises(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("alice", "pw1", Role.VIEWER)
    with pytest.raises(UserAlreadyExistsError):
        store.create("alice", "pw2", Role.ADMIN)


def test_password_never_stored_in_recoverable_form(tmp_path):
    path = tmp_path / "users.json"
    UserStore(path).create("alice", "super-secret-password", Role.ADMIN)
    assert "super-secret-password" not in path.read_text()


def test_user_file_is_only_readable_by_the_owner(tmp_path):
    path = tmp_path / "users.json"
    UserStore(path).create("alice", "pw", Role.VIEWER)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_set_role_changes_future_authentication_result(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("alice", "pw", Role.VIEWER)
    store.set_role("alice", Role.ADMIN)
    user = store.authenticate("alice", "pw")
    assert user.role is Role.ADMIN


def test_set_role_unknown_user_raises(tmp_path):
    store = UserStore(tmp_path / "users.json")
    with pytest.raises(UserNotFoundError):
        store.set_role("nobody", Role.ADMIN)


def test_delete_removes_the_account(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("alice", "pw", Role.VIEWER)
    assert store.delete("alice") is True
    assert store.authenticate("alice", "pw") is None


def test_delete_unknown_user_returns_false(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.delete("nobody") is False


def test_list_users_is_sorted_by_username(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.create("zed", "pw", Role.VIEWER)
    store.create("amy", "pw", Role.ADMIN)
    usernames = [u.username for u in store.list_users()]
    assert usernames == ["amy", "zed"]


def test_is_empty_reflects_current_store(tmp_path):
    store = UserStore(tmp_path / "users.json")
    assert store.is_empty() is True
    store.create("alice", "pw", Role.ADMIN)
    assert store.is_empty() is False


def test_a_second_instance_on_the_same_file_sees_users_created_by_the_first(tmp_path):
    path = tmp_path / "users.json"
    UserStore(path).create("alice", "pw", Role.ADMIN)
    second_instance = UserStore(path)
    user = second_instance.authenticate("alice", "pw")
    assert user is not None


def test_two_users_with_the_same_password_get_different_derived_keys(tmp_path):
    # Distinct random salts must make two identical passwords hash
    # differently on disk - otherwise a leaked file would reveal that
    # two accounts share a password.
    path = tmp_path / "users.json"
    store = UserStore(path)
    store.create("alice", "same-password", Role.VIEWER)
    store.create("bob", "same-password", Role.VIEWER)
    alice, bob = (u for u in store.list_users())
    assert alice.derived_key_hex != bob.derived_key_hex
    assert alice.salt_hex != bob.salt_hex
