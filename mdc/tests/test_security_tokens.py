"""security/tokens.py: bearer-token issuance, revocation, and verification."""

import json

import pytest

from mdc.security.roles import Role
from mdc.security.tokens import TokenStore


def test_issue_then_verify(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.issue("ci")
    assert store.verify(token) is True


def test_verify_rejects_unknown_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.issue("ci")
    assert store.verify("mdc_not-a-real-token") is False


def test_verify_rejects_empty_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.verify("") is False


def test_token_file_never_contains_the_plaintext_token(tmp_path):
    path = tmp_path / "tokens.json"
    token = TokenStore(path).issue("ci")
    assert token not in path.read_text()


def test_token_file_is_only_readable_by_the_owner(tmp_path):
    path = tmp_path / "tokens.json"
    TokenStore(path).issue("ci")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_revoke_invalidates_every_token_under_that_name(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.issue("ci")
    removed = store.revoke("ci")
    assert removed == 1
    assert store.verify(token) is False


def test_revoke_unknown_name_removes_nothing(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.revoke("nope") == 0


def test_issuing_twice_under_the_same_name_keeps_both_tokens_valid(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    a = store.issue("ci")
    b = store.issue("ci")
    assert a != b
    assert store.verify(a) is True
    assert store.verify(b) is True
    assert store.list_names() == ["ci", "ci"]


def test_a_second_instance_on_the_same_file_sees_tokens_issued_by_the_first(tmp_path):
    path = tmp_path / "tokens.json"
    token = TokenStore(path).issue("ci")
    second_instance = TokenStore(path)
    assert second_instance.verify(token) is True


def test_verify_sees_a_token_issued_by_another_instance_after_construction(tmp_path):
    # The scenario that matters for `mdc serve`: a TokenStore object
    # constructed BEFORE a token exists must still recognize it once a
    # separate instance issues and persists it - verify() must not rely
    # on whatever was loaded at __init__ time.
    path = tmp_path / "tokens.json"
    long_lived = TokenStore(path)
    assert long_lived.verify("mdc_whatever") is False

    token = TokenStore(path).issue("issued-later")
    assert long_lived.verify(token) is True


def test_env_tokens_bypass_the_file_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("MDC_API_TOKENS", "fixed-a, fixed-b")
    store = TokenStore(tmp_path / "tokens.json")  # file never created
    assert store.verify("fixed-a") is True
    assert store.verify("fixed-b") is True
    assert store.verify("fixed-c") is False


def test_list_names_reflects_current_store(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.list_names() == []
    store.issue("a")
    store.issue("b")
    assert store.list_names() == ["a", "b"]


def test_issue_defaults_to_admin_role(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.issue("ci")
    record = store.resolve(token)
    assert record is not None
    assert record.role is Role.ADMIN


def test_issue_with_explicit_role_is_honored(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    token = store.issue("readonly-integration", role=Role.VIEWER)
    record = store.resolve(token)
    assert record.role is Role.VIEWER


def test_resolve_returns_none_for_unknown_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.resolve("mdc_not-a-real-token") is None


def test_env_token_resolves_to_admin_role(tmp_path, monkeypatch):
    monkeypatch.setenv("MDC_API_TOKENS", "fixed-a")
    store = TokenStore(tmp_path / "tokens.json")
    record = store.resolve("fixed-a")
    assert record is not None
    assert record.role is Role.ADMIN


def test_a_token_record_predating_roles_loads_as_viewer(tmp_path):
    # Simulates a tokens.json written before the "role" field existed.
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    token = store.issue("legacy", role=Role.ADMIN)
    data = json.loads(path.read_text())
    del data[0]["role"]
    path.write_text(json.dumps(data))

    reloaded = TokenStore(path)
    record = reloaded.resolve(token)
    assert record is not None
    assert record.role is Role.VIEWER
