# mdc-lite v1.2.0 - Release Build Manifest

Built and verified 2026-08-24 on macOS (Apple Silicon), rustc 1.91.1
(stable-aarch64-apple-darwin toolchain), release profile
(`opt-level = "z"`, LTO, `panic = "abort"`, stripped - see
`../Cargo.toml`).

Every file below is verified in **this exact way**: `cargo test`
(23/23 passing, native macOS) before packaging, then `cargo build
--release --target <triple>` per platform, checked for successful
completion and a real, correctly-formatted output binary (`file`
confirms ELF/PE/Mach-O type and architecture for every artifact).
Checksums in `CHECKSUMS.txt` (SHA-256) let you confirm nothing was
altered after this build.

## What's real and included

| Platform | Target triple | Artifact | Size | Verified |
|---|---|---|---|---|
| macOS (Apple Silicon) | `aarch64-apple-darwin` | `macos-arm64/libmdc_lite.dylib` | 348 KB | Built + `cargo test` passing natively on this exact target |
| Windows x86_64 | `x86_64-pc-windows-gnu` | `windows-x86_64/mdc_lite.dll` | 992 KB | Cross-compiled + linked successfully (mingw-w64 toolchain); **not executed** - no Wine available in this environment to run a Windows binary on macOS |
| Windows x86_64 | `x86_64-pc-windows-gnu` | `windows-x86_64/libmdc_lite.dll.a` (import lib) | 5.4 KB | Produced alongside the DLL, for linking against it |
| Android arm64-v8a | `aarch64-linux-android` | `android/arm64-v8a/libmdc_lite.so` | 376 KB | Cross-compiled with the real Android NDK (r29) clang toolchain; ELF confirmed `ARM aarch64, stripped` |
| Android armeabi-v7a | `armv7-linux-androideabi` | `android/armeabi-v7a/libmdc_lite.so` | 252 KB | Same NDK toolchain; ELF confirmed `ARM, EABI5, stripped` |
| Android x86_64 | `x86_64-linux-android` | `android/x86_64/libmdc_lite.so` | 484 KB | Same NDK toolchain; ELF confirmed `x86-64, stripped` (emulator use) |

`include/mdc_lite.h` - the C header, identical across every platform above.

Every artifact above targets **mobile and wearable embedding, and
desktop/CLI interop only** - see "On mdc-lite and desktop" below for
why the macOS/Windows builds exist despite mdc-lite not being a
desktop product itself.

## What's NOT included, and exactly why

**iOS and watchOS: no artifacts.** Apple requires linking against the
real iOS/watchOS SDK, which ships only inside the full Xcode.app (not
the Command Line Tools, which is all this build environment has -
confirmed via `xcodebuild -version` and `xcrun --sdk iphoneos
--show-sdk-path` both failing here). There is no legitimate workaround
for this restriction - it isn't a missing package, it's an Apple
distribution requirement. Build on a Mac with full Xcode installed
using the commands in `../README.md`'s iOS/watchOS section; the Rust
source itself is fully portable (the same crate that builds clean for
macOS/Windows/Android), so this is a tooling gap on this specific
machine, not a code gap.

**Windows DLL: compiled and linked, not executed.** I could not verify
runtime behavior (only that mingw-w64 successfully produced a valid
PE32+ DLL) because this environment has no Wine or Windows machine to
run it on. Treat the compile success as real evidence the code is
portable, not as proof of correct runtime behavior on Windows - if
this matters to you, run `cargo test --target
x86_64-pc-windows-gnu` (needs Wine) or the DLL itself on a real Windows
machine before shipping.

**Android .so files: linked and inspected, not executed on a device or
emulator.** No Android emulator was available in this environment to
actually load and exercise the library at runtime.

## Why there's no macOS static library (`.a`) here

`cargo build --release` also produces `libmdc_lite.a`, but it isn't
included in this bundle - a static archive bundles *unstripped* object
code for every dependency (14 MB for this crate), which is not what
ends up in a consuming app (the final app's linker performs dead-code
elimination against it, the same way it does for the rest of the
binary) and would misrepresent this project's actual footprint sitting
next to a 353 KB `.dylib`. If you need it, `cargo build --release`
from source produces it directly - it's not a hard-to-reproduce
artifact, just one not worth bundling.

## On mdc-lite and desktop

mdc-lite is a **mobile/wearable embeddable library** - it has no CLI
and no standalone desktop app, and never has. The macOS and Windows
builds in this bundle exist for two reasons that are not "mdc-lite as
a desktop product": (1) so a desktop-hosted companion app for a
mobile/wearable product can embed the same store format on the desktop
side, and (2) so MDC Platform's own CLI can, on a roadmap basis, open
an mdc-lite store directory directly when a phone or wearable is
connected to a Windows machine (tracked as a Phase 2 item, not yet
built - see `../../DNA-STORAGE-WHITEPAPER.md`). Neither reason makes
mdc-lite itself a desktop product.

## What changed since v1.1.0

**On-disk format change (breaking):** every entry's encrypted bytes
(`[nonce][ciphertext][tag]`, unchanged) are now DNA-encoded (`src/dna.rs`,
the same 2-bit-per-base `00→A 01→C 10→G 11→T` mapping MDC Platform's
DNA tier uses) before being written to disk - so a v1.2.0 store's files
are ACGT text, not raw binary, matching the DNA-inspired storage model
across both mdc-lite and MDC Platform rather than just this crate's
name alluding to it. **v1.1.0 stores are not readable by v1.2.0 and
vice versa** - there is no migration path for this prototype-stage
format change; re-write existing entries with the new version if you
have any. The DNA encoding is applied to ciphertext, never plaintext -
the 2-bit mapping itself is public, so encoding plaintext directly
would only be obfuscation, not protection; encrypting first is what
actually makes a file unreadable without the key.

23 tests now (up from 14): 6 new tests in `src/dna.rs` for the
encoding round-trip, plus 3 new tests in `src/lib.rs` confirming the
on-disk file is genuinely ACGT text, doesn't contain the plaintext
value, and that a corrupted (non-ACGT) file produces a clean
`Corrupt` error rather than a panic.

## Toolchain versions used

- `rustc 1.91.1` / `cargo 1.91.1` (rustup `stable-aarch64-apple-darwin`)
- `mingw-w64 14.0.0_3` (Homebrew) for the Windows GNU target
- Android NDK r29 (Homebrew cask `android-ndk`), API level 24 minimum
  (`aarch64-linux-android24-clang` etc. - Android 7.0+)
