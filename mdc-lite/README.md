# mdc-lite

A tiny embeddable encrypted key-value store, meant to be bundled directly
into a mobile or wearable app - the native counterpart to
[MDC](../mdc/)'s Python `StorageBackend` abstraction, built from scratch
for a completely different constraint set: no server process, no
language runtime, small enough to link straight into an app binary.

Standalone by design for now: it does not talk to the Python MDC server
over a network, and has no built-in sync protocol. It's a local,
on-device store.

## Why this exists, and why it isn't "the Python project, but smaller"

MDC's existing storage engine is Python + FastAPI + DuckDB - a server
stack. That combination cannot run on watchOS or Wear OS at all (no
Python runtime, and app review on both platforms requires native
frameworks), and it's heavy even to bundle into a phone app. Trimming
dependencies from that stack doesn't get you to "runs on a watch" -
it needs a different implementation, sharing the *idea*
(put/get/exists/delete, encrypted, tiered) but not the code. Rust is
the standard choice for this: no runtime, compiles to a small native
library, and cross-compiles to iOS/watchOS and Android/Wear OS from one
codebase.

## Security model

Every value - and every key name - is encrypted with
**XChaCha20-Poly1305** (a 256-bit-key, 192-bit-nonce authenticated
cipher) using a key **you supply**. This crate never generates, stores,
or has an opinion about that key's custody - real key security on a
phone or watch comes from the platform's secure hardware (iOS Secure
Enclave / Keychain, Android Keystore / StrongBox), which is
platform-specific code this crate deliberately doesn't own. See
[Platform key custody](#platform-key-custody-not-part-of-this-crate)
below.

Without the correct key, someone holding the raw store directory sees
only random-looking filenames (key names are hashed with a keyed
BLAKE3 hash, not stored in the clear) and authenticated ciphertext
blobs. There is no recovery path if the key is lost - that's the
tradeoff, by design.

**On "quantum encryption":** real quantum key distribution needs
dedicated fiber/free-space hardware between two fixed endpoints - it's
a network-link technology, not something that runs on a device. What
actually matters for data at rest is symmetric key strength: AES-256
and XChaCha20 (both 256-bit) are already resistant to the quantum
speedup that exists (Grover's algorithm only halves an *n*-bit key's
effective strength, so 256 bits stays enormous). Quantum computers
threaten *asymmetric* crypto (RSA/ECC, via Shor's algorithm), which
this crate doesn't use at all - key exchange between devices isn't
something this crate does (it's out of scope for a local, standalone
store), so there's no asymmetric attack surface here to begin with.

## On-disk format

One file per entry: `<blake3_keyed_hash(store_key, logical_key) as hex>.mdclite`,
containing `[24-byte nonce][ciphertext][16-byte auth tag]`. The
plaintext under that ciphertext is `[u16 LE key_len][key_bytes][value_bytes]`
- the logical key travels inside the encrypted payload (not the
filename), so `list_keys()` can recover it after decrypting while a
raw directory listing alone reveals nothing. See `src/lib.rs`'s module
doc for the full threat-model writeup.

## API

```rust
use mdc_lite::LiteStore;

let key: [u8; 32] = /* from platform secure storage */;
let store = LiteStore::open("/path/to/store/dir", key)?;

store.put("diary/2026-08-24", b"...")?;
let value = store.get("diary/2026-08-24")?;
store.exists("diary/2026-08-24"); // bool
store.delete("diary/2026-08-24")?;
let all_keys = store.list_keys()?; // Vec<String>
```

A C ABI (`src/ffi.rs`, header at `include/mdc_lite.h`) exposes the same
five operations for Swift/Kotlin/any C-compatible caller.

## Building

Native (this machine, for `cargo test` / development):

```bash
cargo build --release
```

Verified in this environment: `cargo test` (14/14 passing) and a
size-optimized native release build - **348 KB** for the stripped
`.dylib` (`opt-level = "z"`, LTO, `panic = "abort"`, symbols stripped;
see `Cargo.toml`'s `[profile.release]`). The `.a` static archive is
larger (~7.6 MB) because it bundles unstripped object code for every
dependency - that is *not* what ends up in your app; the final app
linker performs dead-code elimination against the static lib the same
way it does for the rest of your binary, so the real per-app cost is
much closer to the `.dylib` number above.

**iOS/watchOS/Android cross-compilation is documented below but not
executed in this environment** - it only has Xcode Command Line Tools
(no iOS/watchOS SDK) and no Android NDK, so I could not verify an
actual cross-compiled build end-to-end here. The instructions below are
the standard, well-established approach; verify on a machine with the
real toolchains before shipping.

### iOS + watchOS (needs rustup and full Xcode, not just Command Line Tools)

```bash
rustup target add aarch64-apple-ios aarch64-apple-ios-sim aarch64-apple-watchos aarch64-apple-watchos-sim

cargo build --release --target aarch64-apple-ios          # device
cargo build --release --target aarch64-apple-ios-sim      # Apple Silicon simulator
cargo build --release --target aarch64-apple-watchos      # watch device
cargo build --release --target aarch64-apple-watchos-sim  # watch simulator
```

watchOS Rust targets are less mature (Tier 3) than the iOS ones - some
toolchain/Rust version combinations need `-Z build-std` on nightly to
build `core`/`alloc` for them. Check current `rustup target list`
output against your Rust version before relying on this.

Combine the per-architecture `.a` static libraries into an XCFramework
(`xcodebuild -create-xcframework`) and add `include/mdc_lite.h` as its
bridging header for Swift to import.

### Android + Wear OS (needs the Android NDK)

```bash
rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android

# Point cargo at the NDK's per-architecture clang wrappers, e.g. via
# cargo-ndk (`cargo install cargo-ndk`):
cargo ndk -t arm64-v8a -t armeabi-v7a -t x86_64 -o jniLibs build --release
```

Wear OS runs on Android, so the same `.so` outputs work for both - a
watch face or Wear OS app links this the same way a phone app does.

## Platform key custody (not part of this crate)

This crate takes a 32-byte key and does nothing else with key
management - obtaining and storing that key securely is the app's job,
using the platform's own secure storage. Illustrative sketches below
(not compiled/verified here - treat as a starting point, not
copy-paste-ready code):

**iOS/watchOS** - generate a random key once, store it in the Keychain
with hardware-backed protection:

```swift
import Security

func getOrCreateStoreKey() -> Data {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrAccount as String: "mdc-lite-store-key",
        kSecReturnData as String: true,
    ]
    var item: AnyObject?
    if SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess {
        return item as! Data
    }

    var keyBytes = Data(count: 32)
    _ = keyBytes.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 32, $0.baseAddress!) }

    let addQuery: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrAccount as String: "mdc-lite-store-key",
        kSecValueData as String: keyBytes,
        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    ]
    SecItemAdd(addQuery as CFDictionary, nil)
    return keyBytes
}
```

**Android/Wear OS** - use the Android Keystore to generate a
hardware-backed AES key and wrap a random store key with it (or, more
simply, store the random key in `EncryptedSharedPreferences`, which
itself is backed by a Keystore-protected key):

```kotlin
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

fun getOrCreateStoreKey(context: Context): ByteArray {
    val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()
    val prefs = EncryptedSharedPreferences.create(
        context, "mdc_lite_key_prefs", masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )
    prefs.getString("store_key", null)?.let { return Base64.decode(it, Base64.NO_WRAP) }

    val key = ByteArray(32).also { SecureRandom().nextBytes(it) }
    prefs.edit().putString("store_key", Base64.encodeToString(key, Base64.NO_WRAP)).apply()
    return key
}
```

## What this deliberately is not

- No query language, no schema, no filters - it's a raw encrypted
  key-value store. Layer your own indexing on top if you need it.
- No compaction or space reclamation beyond deleting a file on
  `delete()` - there's no background process on a battery/resource
  constrained device.
- No multi-device sync. No network code at all, actually.
- No built-in key rotation - if you need it, `list_keys()` +
  re-`put()` under a new `LiteStore` opened with the new key, then
  delete the old store directory.
