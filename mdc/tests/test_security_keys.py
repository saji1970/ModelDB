"""security/keys.py: the at-rest key backing the DNA storage tier."""

import pytest

from mdc.security.keys import KEY_LEN, load_or_create_dna_key


def test_generates_and_persists_a_key(tmp_path):
    key_path = tmp_path / "dna.key"
    key = load_or_create_dna_key(key_path)
    assert len(key) == KEY_LEN
    assert key_path.exists()
    assert load_or_create_dna_key(key_path) == key  # second call reuses the same key


def test_key_file_is_only_readable_by_the_owner(tmp_path):
    key_path = tmp_path / "dna.key"
    load_or_create_dna_key(key_path)
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_env_var_takes_precedence_over_the_file(tmp_path, monkeypatch):
    key_path = tmp_path / "dna.key"
    load_or_create_dna_key(key_path)  # creates a file-backed key first
    env_key = "11" * KEY_LEN
    monkeypatch.setenv("MDC_DNA_KEY", env_key)
    assert load_or_create_dna_key(key_path) == bytes.fromhex(env_key)


def test_env_var_wrong_length_raises(monkeypatch):
    monkeypatch.setenv("MDC_DNA_KEY", "ab" * 16)  # 16 bytes, not 32
    with pytest.raises(ValueError):
        load_or_create_dna_key()


def test_env_var_non_hex_raises(monkeypatch):
    monkeypatch.setenv("MDC_DNA_KEY", "not hex at all!!")
    with pytest.raises(ValueError):
        load_or_create_dna_key()


def test_two_different_paths_get_different_keys(tmp_path):
    key_a = load_or_create_dna_key(tmp_path / "a.key")
    key_b = load_or_create_dna_key(tmp_path / "b.key")
    assert key_a != key_b
