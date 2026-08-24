//! mdc-lite: a tiny embeddable encrypted key-value store.
//!
//! The native counterpart to MDC's Python `StorageBackend` abstraction
//! (put/get/exists/delete/search), rebuilt from scratch for a completely
//! different constraint set: no server, no runtime dependency, small
//! enough to bundle straight into an iOS/watchOS or Android/Wear OS app.
//!
//! ## Threat model / what "unreadable outside the app" actually means
//!
//! This crate encrypts every value (and every key name) with
//! XChaCha20-Poly1305 using a 256-bit key **you supply** - it never
//! generates, stores, or has any opinion about that key's custody.
//! That's deliberate: real key security on a phone or watch comes from
//! the platform's secure hardware (iOS Secure Enclave / Keychain,
//! Android Keystore / StrongBox), which is platform-specific code this
//! crate can't and shouldn't own. Without the correct key, an attacker
//! holding the raw store directory sees only random-looking filenames
//! and authenticated ciphertext blobs - no key names, no values, no
//! way to tell how many distinct keys exist versus how many files.
//! Losing or leaking the key is equivalent to losing the data; there is
//! no recovery path here, by design (see `docs/PLATFORM_INTEGRATION.md`
//! for how the key itself should be obtained/stored on each platform).
//!
//! ## On-disk format
//!
//! One file per entry, named `blake3_keyed_hash(store_key, logical_key)`
//! (hex-encoded) so filenames don't leak plaintext key names. Each file
//! is `[24-byte nonce][ciphertext][16-byte auth tag]`, where the
//! plaintext under that ciphertext is `[u16 LE key_len][key_bytes][value_bytes]`.
//! The logical key travels inside the encrypted payload rather than the
//! filename, so `list_keys()` can recover it after decrypting while a
//! directory listing alone reveals nothing.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use chacha20poly1305::aead::{Aead, KeyInit, OsRng};
use chacha20poly1305::{AeadCore, XChaCha20Poly1305, XNonce};

pub const KEY_LEN: usize = 32;
const NONCE_LEN: usize = 24;
const TAG_LEN: usize = 16;
const FILE_EXT: &str = "mdclite";

#[derive(Debug)]
pub enum LiteStoreError {
    NotFound,
    Io(io::Error),
    /// Decryption/authentication failed - wrong key, corrupted bytes, or
    /// a tampered file. Never distinguished further: telling an attacker
    /// *which* of those happened is itself information leakage.
    Crypto,
    /// Decryption succeeded (so the key was right and the bytes weren't
    /// tampered with) but the plaintext payload itself is malformed -
    /// should never happen from an entry this crate wrote itself.
    Corrupt,
}

impl std::fmt::Display for LiteStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LiteStoreError::NotFound => write!(f, "no entry found for that key"),
            LiteStoreError::Io(e) => write!(f, "I/O error: {e}"),
            LiteStoreError::Crypto => write!(f, "decryption failed (wrong key or corrupted/tampered data)"),
            LiteStoreError::Corrupt => write!(f, "entry payload is malformed"),
        }
    }
}

impl std::error::Error for LiteStoreError {}

impl From<io::Error> for LiteStoreError {
    fn from(e: io::Error) -> Self {
        if e.kind() == io::ErrorKind::NotFound {
            LiteStoreError::NotFound
        } else {
            LiteStoreError::Io(e)
        }
    }
}

/// A single encrypted, directory-backed key-value store.
///
/// Cheap to open (just resolves a directory path and holds the key in
/// memory); every operation is a single file read or write, so this
/// deliberately has no in-memory cache, background thread, or
/// compaction process to reason about on a resource-constrained device.
pub struct LiteStore {
    dir: PathBuf,
    key: [u8; KEY_LEN],
}

impl LiteStore {
    /// Opens (creating if necessary) an encrypted store rooted at `dir`,
    /// using `key` for both filename derivation and payload encryption.
    pub fn open(dir: impl AsRef<Path>, key: [u8; KEY_LEN]) -> Result<Self, LiteStoreError> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(&dir)?;
        Ok(Self { dir, key })
    }

    fn cipher(&self) -> XChaCha20Poly1305 {
        XChaCha20Poly1305::new((&self.key).into())
    }

    fn path_for(&self, key: &str) -> PathBuf {
        let hash = blake3::keyed_hash(&self.key, key.as_bytes());
        self.dir.join(format!("{}.{FILE_EXT}", hash.to_hex()))
    }

    /// Encrypts and stores `value` under `key`, overwriting any existing
    /// entry for that key.
    pub fn put(&self, key: &str, value: &[u8]) -> Result<(), LiteStoreError> {
        let key_bytes = key.as_bytes();
        if key_bytes.len() > u16::MAX as usize {
            return Err(LiteStoreError::Corrupt);
        }
        let mut plaintext = Vec::with_capacity(2 + key_bytes.len() + value.len());
        plaintext.extend_from_slice(&(key_bytes.len() as u16).to_le_bytes());
        plaintext.extend_from_slice(key_bytes);
        plaintext.extend_from_slice(value);

        let nonce = XChaCha20Poly1305::generate_nonce(&mut OsRng);
        let ciphertext = self
            .cipher()
            .encrypt(&nonce, plaintext.as_ref())
            .map_err(|_| LiteStoreError::Crypto)?;

        let mut file_bytes = Vec::with_capacity(NONCE_LEN + ciphertext.len());
        file_bytes.extend_from_slice(nonce.as_slice());
        file_bytes.extend_from_slice(&ciphertext);

        let path = self.path_for(key);
        // Write to a temp file and rename - never leaves a partially
        // written (and therefore un-authenticatable, effectively lost)
        // entry on disk if the app is killed mid-write.
        let tmp_path = path.with_extension(format!("{FILE_EXT}.tmp"));
        fs::write(&tmp_path, &file_bytes)?;
        fs::rename(&tmp_path, &path)?;
        Ok(())
    }

    /// Decrypts and returns the value stored under `key`.
    pub fn get(&self, key: &str) -> Result<Vec<u8>, LiteStoreError> {
        let (_, value) = self.read_entry(&self.path_for(key))?;
        Ok(value)
    }

    pub fn exists(&self, key: &str) -> bool {
        self.path_for(key).is_file()
    }

    pub fn delete(&self, key: &str) -> Result<(), LiteStoreError> {
        match fs::remove_file(self.path_for(key)) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()), // delete is idempotent
            Err(e) => Err(e.into()),
        }
    }

    /// Every logical key currently stored - requires decrypting each
    /// entry's small header (there is no plaintext index to keep in
    /// sync, on purpose: fewer places for the store to become
    /// inconsistent with itself after an interrupted write).
    ///
    /// Unlike [`get`](Self::get), a single entry this store's key can't
    /// decrypt (a foreign file that landed in the directory, a
    /// corrupted write) is silently skipped rather than failing the
    /// whole call - enumeration is a best-effort inventory, and letting
    /// one bad entry hide every other valid key would be a worse
    /// failure mode on a device where you generally want the app to
    /// keep working. `get(key)` on a *specific* key still fails loud,
    /// which is the operation where that matters.
    pub fn list_keys(&self) -> Result<Vec<String>, LiteStoreError> {
        let mut keys = Vec::new();
        let entries = match fs::read_dir(&self.dir) {
            Ok(entries) => entries,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(keys),
            Err(e) => return Err(e.into()),
        };
        for entry in entries {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some(FILE_EXT) {
                continue;
            }
            if let Ok((key, _)) = self.read_entry(&path) {
                keys.push(key);
            }
        }
        Ok(keys)
    }

    fn read_entry(&self, path: &Path) -> Result<(String, Vec<u8>), LiteStoreError> {
        let file_bytes = fs::read(path)?;
        if file_bytes.len() < NONCE_LEN + TAG_LEN {
            return Err(LiteStoreError::Corrupt);
        }
        let (nonce_bytes, ciphertext) = file_bytes.split_at(NONCE_LEN);
        let nonce = XNonce::from_slice(nonce_bytes);
        let plaintext = self
            .cipher()
            .decrypt(nonce, ciphertext)
            .map_err(|_| LiteStoreError::Crypto)?;

        if plaintext.len() < 2 {
            return Err(LiteStoreError::Corrupt);
        }
        let key_len = u16::from_le_bytes([plaintext[0], plaintext[1]]) as usize;
        if plaintext.len() < 2 + key_len {
            return Err(LiteStoreError::Corrupt);
        }
        let key = String::from_utf8(plaintext[2..2 + key_len].to_vec()).map_err(|_| LiteStoreError::Corrupt)?;
        let value = plaintext[2 + key_len..].to_vec();
        Ok((key, value))
    }
}

pub mod ffi;

#[cfg(test)]
mod tests {
    use super::*;

    fn key(byte: u8) -> [u8; KEY_LEN] {
        [byte; KEY_LEN]
    }

    #[test]
    fn put_then_get_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.put("diary/2026-08-24", b"hello wearable").unwrap();
        assert_eq!(store.get("diary/2026-08-24").unwrap(), b"hello wearable");
    }

    #[test]
    fn get_unknown_key_is_not_found() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        assert!(matches!(store.get("nope"), Err(LiteStoreError::NotFound)));
    }

    #[test]
    fn exists_reflects_put_and_delete() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        assert!(!store.exists("k"));
        store.put("k", b"v").unwrap();
        assert!(store.exists("k"));
        store.delete("k").unwrap();
        assert!(!store.exists("k"));
    }

    #[test]
    fn delete_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.delete("never-existed").unwrap(); // must not error
    }

    #[test]
    fn put_overwrites_existing_value() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.put("k", b"first").unwrap();
        store.put("k", b"second").unwrap();
        assert_eq!(store.get("k").unwrap(), b"second");
    }

    #[test]
    fn list_keys_recovers_original_key_names() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.put("alpha", b"1").unwrap();
        store.put("beta", b"2").unwrap();
        let mut keys = store.list_keys().unwrap();
        keys.sort();
        assert_eq!(keys, vec!["alpha".to_string(), "beta".to_string()]);
    }

    #[test]
    fn wrong_key_cannot_decrypt() {
        let dir = tempfile::tempdir().unwrap();
        let writer = LiteStore::open(dir.path(), key(1)).unwrap();
        writer.put("secret", b"classified").unwrap();

        // Same directory, different key - simulates another app (or an
        // attacker without Secure Enclave/Keystore access) trying to
        // read this app's store.
        let reader = LiteStore::open(dir.path(), key(2)).unwrap();
        // Filenames are keyed by the store's own key, so a different key
        // derives a different filename - list_keys() (which must
        // decrypt to recover names) also can't succeed, since no file
        // it looks for was ever written under key(2)'s naming scheme.
        assert!(reader.list_keys().unwrap().is_empty());
        assert!(matches!(reader.get("secret"), Err(LiteStoreError::NotFound)));
    }

    #[test]
    fn tampered_ciphertext_is_detected_not_silently_accepted() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.put("k", b"original value").unwrap();

        // Flip a byte in the middle of the on-disk file (well past the
        // nonce) - simulates corruption or tampering.
        let path = store.path_for("k");
        let mut bytes = fs::read(&path).unwrap();
        let mid = bytes.len() / 2;
        bytes[mid] ^= 0xFF;
        fs::write(&path, &bytes).unwrap();

        assert!(matches!(store.get("k"), Err(LiteStoreError::Crypto)));
    }

    #[test]
    fn two_stores_in_different_directories_do_not_collide() {
        let dir_a = tempfile::tempdir().unwrap();
        let dir_b = tempfile::tempdir().unwrap();
        let store_a = LiteStore::open(dir_a.path(), key(1)).unwrap();
        let store_b = LiteStore::open(dir_b.path(), key(1)).unwrap();

        store_a.put("k", b"from-a").unwrap();
        assert!(matches!(store_b.get("k"), Err(LiteStoreError::NotFound)));
    }

    #[test]
    fn empty_value_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        store.put("k", b"").unwrap();
        assert_eq!(store.get("k").unwrap(), b"");
    }

    #[test]
    fn binary_value_round_trips_exactly() {
        let dir = tempfile::tempdir().unwrap();
        let store = LiteStore::open(dir.path(), key(1)).unwrap();
        let value: Vec<u8> = (0..=255u8).collect();
        store.put("k", &value).unwrap();
        assert_eq!(store.get("k").unwrap(), value);
    }
}
