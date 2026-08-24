# mdc-lite

**[Website & docs](../docs/index.html)** - the same content as this
README, plus the platform download/build matrix, laid out for browsing
rather than reading top to bottom (once GitHub Pages is enabled for
this repo, that link resolves to a real hosted site).

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

Verified in this environment: `cargo test` (14/14 passing), zero
clippy warnings, and a size-optimized native release build - **345 KB**
for the stripped `.dylib` (`opt-level = "z"`, LTO, `panic = "abort"`,
symbols stripped; see `Cargo.toml`'s `[profile.release]`). The `.a`
static archive is larger (~17 MB) because it bundles unstripped object
code for every dependency - that is *not* what ends up in your app; the
final app linker performs dead-code elimination against the static lib
the same way it does for the rest of your binary, so the real per-app
cost is much closer to the `.dylib` number above.

**Windows and Android are also actually cross-compiled and verified in
this environment** - see [dist/BUILD_INFO.md](dist/BUILD_INFO.md) for
exact sizes, toolchain versions, and precisely what "verified" does and
doesn't mean for each (e.g. Windows: compiles and links to a real DLL,
not executed - no Wine available here to run it). **iOS/watchOS is not
buildable here at all** - Apple requires linking against the real
SDK, which ships only inside full Xcode.app, and this environment has
only the Command Line Tools (confirmed via `xcodebuild -version`
failing). That's not a missing package, it's an Apple distribution
requirement with no workaround; build it on a Mac with full Xcode.

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

### Windows (verified: mingw-w64, no Visual Studio needed)

```bash
rustup target add x86_64-pc-windows-gnu
brew install mingw-w64   # provides the x86_64-w64-mingw32-gcc linker

cargo build --release --target x86_64-pc-windows-gnu
```

Produces `mdc_lite.dll` + `libmdc_lite.dll.a` (the import library to
link against). This is a real, standard C ABI DLL - loadable from an
MSVC-built consumer too, since the PE/C-ABI boundary doesn't care which
compiler produced the DLL.

### Android + Wear OS (needs the Android NDK)

```bash
rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android
brew install --cask android-ndk

export ANDROID_NDK_HOME="/opt/homebrew/share/android-ndk"   # brew's install path
NDK_BIN="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/darwin-x86_64/bin"

# API level 24 (Android 7.0+) minimum - adjust the version suffix below
# to target a different minSdkVersion.
export CC_aarch64_linux_android="$NDK_BIN/aarch64-linux-android24-clang"
export AR_aarch64_linux_android="$NDK_BIN/llvm-ar"
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$NDK_BIN/aarch64-linux-android24-clang"
export CC_armv7_linux_androideabi="$NDK_BIN/armv7a-linux-androideabi24-clang"
export AR_armv7_linux_androideabi="$NDK_BIN/llvm-ar"
export CARGO_TARGET_ARMV7_LINUX_ANDROIDEABI_LINKER="$NDK_BIN/armv7a-linux-androideabi24-clang"
export CC_x86_64_linux_android="$NDK_BIN/x86_64-linux-android24-clang"
export AR_x86_64_linux_android="$NDK_BIN/llvm-ar"
export CARGO_TARGET_X86_64_LINUX_ANDROID_LINKER="$NDK_BIN/x86_64-linux-android24-clang"

cargo build --release --target aarch64-linux-android      # arm64-v8a (real devices)
cargo build --release --target armv7-linux-androideabi    # armeabi-v7a (older devices)
cargo build --release --target x86_64-linux-android        # x86_64 (emulator)
```

Both sets of variables are load-bearing, for two different reasons -
**and this was verified the hard way**: an earlier version of this
README only listed the `CC_*`/`AR_*` pair, and following those
instructions exactly reproduces a real failure, not a working build.

- `CC_*`/`AR_*` - this crate's `blake3` dependency runs its own C
  build script (via the `cc` crate) that needs to find a working C
  compiler independently of cargo's own linker selection; without
  these, that step fails with `ToolNotFound` even when everything else
  is configured correctly.
- `CARGO_TARGET_<TRIPLE>_LINKER` - without this, cargo's own default
  linker resolution for these targets can fall through to whatever
  plain `clang`/`ld` your system happens to have on `PATH` (on macOS,
  Apple's own linker) instead of the NDK's - which fails with a
  confusing `ld: unknown options: --version-script=...` error, since
  Apple's linker doesn't understand the ELF-target flags rustc passes
  it. `CC_*`/`AR_*` alone does not fix this; it's a separate variable
  cargo itself reads.

Copy each `.so` into your app's `jniLibs/<abi>/` directory
(`arm64-v8a`, `armeabi-v7a`, `x86_64`). Wear OS runs on Android, so the
same outputs
work for a watch face or Wear OS app the same way they do for a phone
app.

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
