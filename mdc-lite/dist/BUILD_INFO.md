# mdc-lite v1.1.0 - Release Build Manifest

Built and verified 2026-08-24 on macOS (Apple Silicon), rustc 1.91.1
(stable-aarch64-apple-darwin toolchain), release profile
(`opt-level = "z"`, LTO, `panic = "abort"`, stripped - see
`../Cargo.toml`).

Every file below is verified in **this exact way**: `cargo test`
(14/14 passing, native macOS) before packaging, then `cargo build
--release --target <triple>` per platform, checked for successful
completion and a real, correctly-formatted output binary (`file`
confirms ELF/PE/Mach-O type and architecture for every artifact).
Checksums in `CHECKSUMS.txt` (SHA-256) let you confirm nothing was
altered after this build.

## What's real and included

| Platform | Target triple | Artifact | Size | Verified |
|---|---|---|---|---|
| macOS (Apple Silicon) | `aarch64-apple-darwin` | `macos-arm64/libmdc_lite.dylib` | 345 KB | Built + `cargo test` passing natively on this exact target |
| Windows x86_64 | `x86_64-pc-windows-gnu` | `windows-x86_64/mdc_lite.dll` | 991 KB | Cross-compiled + linked successfully (mingw-w64 toolchain); **not executed** - no Wine available in this environment to run a Windows binary on macOS |
| Windows x86_64 | `x86_64-pc-windows-gnu` | `windows-x86_64/libmdc_lite.dll.a` (import lib) | 5.4 KB | Produced alongside the DLL, for linking against it |
| Android arm64-v8a | `aarch64-linux-android` | `android/arm64-v8a/libmdc_lite.so` | 372 KB | Cross-compiled with the real Android NDK (r29) clang toolchain; ELF confirmed `ARM aarch64, stripped` |
| Android armeabi-v7a | `armv7-linux-androideabi` | `android/armeabi-v7a/libmdc_lite.so` | 250 KB | Same NDK toolchain; ELF confirmed `ARM, EABI5, stripped` |
| Android x86_64 | `x86_64-linux-android` | `android/x86_64/libmdc_lite.so` | 482 KB | Same NDK toolchain; ELF confirmed `x86-64, stripped` (emulator use) |

`include/mdc_lite.h` - the C header, identical across every platform above.

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
code for every dependency (17 MB for this crate), which is not what
ends up in a consuming app (the final app's linker performs dead-code
elimination against it, the same way it does for the rest of the
binary) and would misrepresent this project's actual footprint sitting
next to a 345 KB `.dylib`. If you need it, `cargo build --release`
from source produces it directly - it's not a hard-to-reproduce
artifact, just one not worth bundling.

## What changed since v0.1.0

No source changes to the crate itself - `src/lib.rs` and `src/ffi.rs`
are unchanged, same 14 tests, same API. This release exists to fix a
real bug caught while rebuilding these exact artifacts: rebuilding the
Android targets with the *documented* env vars (`CC_*`/`AR_*` only, no
`CARGO_TARGET_<TRIPLE>_LINKER`) failed outright with `ld: unknown
options: --version-script=...` - cargo's own linker resolution for
these targets was falling through to the host's system linker (Apple's
ld, which doesn't understand the ELF-target flags rustc was passing
it), not the NDK's. `CC_*`/`AR_*` alone was never sufficient; it only
covers the `blake3` dependency's own C build script, a separate step
from cargo's Rust-level link. The README and docs site's Android
build instructions were wrong in v0.1.0 and are fixed as of this
release - verified by reproducing the failure, then reproducing the
fix, before documenting either.

## Toolchain versions used

- `rustc 1.91.1` / `cargo 1.91.1` (rustup `stable-aarch64-apple-darwin`)
- `mingw-w64 14.0.0_3` (Homebrew) for the Windows GNU target
- Android NDK r29 (Homebrew cask `android-ndk`), API level 24 minimum
  (`aarch64-linux-android24-clang` etc. - Android 7.0+)
