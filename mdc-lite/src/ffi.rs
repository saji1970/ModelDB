//! C ABI surface for non-Rust callers (Swift via a bridging header,
//! Kotlin via a thin JNI shim, or any other C-compatible caller).
//!
//! Deliberately minimal and defensive: every function validates its raw
//! pointers before touching them and turns any Rust panic into an error
//! code rather than letting it unwind across the FFI boundary (which is
//! undefined behavior). No function ever returns a Rust-owned pointer
//! the caller is expected to free with anything other than
//! [`mdc_lite_free_buffer`]/[`mdc_lite_close`] - that pairing is the one
//! ownership contract callers need to know.

use std::ffi::{c_char, CStr};
use std::panic::{self, AssertUnwindSafe};
use std::ptr;
use std::slice;

use crate::{LiteStore, LiteStoreError, KEY_LEN};

#[repr(i32)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MdcLiteStatus {
    Ok = 0,
    NotFound = 1,
    CryptoError = 2,
    IoError = 3,
    InvalidArgument = 4,
    InternalError = 5,
}

impl From<LiteStoreError> for MdcLiteStatus {
    fn from(e: LiteStoreError) -> Self {
        match e {
            LiteStoreError::NotFound => MdcLiteStatus::NotFound,
            LiteStoreError::Crypto => MdcLiteStatus::CryptoError,
            LiteStoreError::Io(_) => MdcLiteStatus::IoError,
            LiteStoreError::Corrupt => MdcLiteStatus::CryptoError,
        }
    }
}

/// Opens (creating if needed) a store at `dir_path` (a null-terminated
/// UTF-8 C string) using the 32 bytes at `key_ptr` as the store key.
/// Returns a heap-allocated handle to be passed to every other
/// function and eventually released with [`mdc_lite_close`], or a null
/// pointer if `dir_path`/`key_ptr` are null or `dir_path` isn't valid
/// UTF-8.
///
/// # Safety
/// `dir_path` must be a valid, null-terminated C string (or null).
/// `key_ptr` must point at [`KEY_LEN`] readable bytes (or be null).
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_open(dir_path: *const c_char, key_ptr: *const u8) -> *mut LiteStore {
    if dir_path.is_null() || key_ptr.is_null() {
        return ptr::null_mut();
    }
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let dir = unsafe { CStr::from_ptr(dir_path) }.to_str().ok()?;
        let mut key = [0u8; KEY_LEN];
        unsafe { ptr::copy_nonoverlapping(key_ptr, key.as_mut_ptr(), KEY_LEN) };
        LiteStore::open(dir, key).ok()
    }));
    match result {
        Ok(Some(store)) => Box::into_raw(Box::new(store)),
        _ => ptr::null_mut(),
    }
}

/// Releases a handle returned by [`mdc_lite_open`]. Safe to call with
/// null (no-op). Never call twice on the same pointer.
///
/// # Safety
/// `store` must be a pointer previously returned by [`mdc_lite_open`]
/// (or null) that hasn't already been passed to this function.
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_close(store: *mut LiteStore) {
    if store.is_null() {
        return;
    }
    let _ = panic::catch_unwind(AssertUnwindSafe(|| unsafe {
        drop(Box::from_raw(store));
    }));
}

/// Encrypts and stores `value_ptr[..value_len]` under `key`.
///
/// # Safety
/// `store` must be a live pointer from [`mdc_lite_open`]. `key` must be
/// a valid null-terminated C string. `value_ptr` must point at
/// `value_len` readable bytes (or be null iff `value_len == 0`).
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_put(
    store: *mut LiteStore,
    key: *const c_char,
    value_ptr: *const u8,
    value_len: usize,
) -> MdcLiteStatus {
    if store.is_null() || key.is_null() || (value_ptr.is_null() && value_len > 0) {
        return MdcLiteStatus::InvalidArgument;
    }
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let store = unsafe { &*store };
        let key = match unsafe { CStr::from_ptr(key) }.to_str() {
            Ok(k) => k,
            Err(_) => return MdcLiteStatus::InvalidArgument,
        };
        let value = if value_len == 0 { &[][..] } else { unsafe { slice::from_raw_parts(value_ptr, value_len) } };
        match store.put(key, value) {
            Ok(()) => MdcLiteStatus::Ok,
            Err(e) => e.into(),
        }
    }));
    result.unwrap_or(MdcLiteStatus::InternalError)
}

/// Decrypts the value stored under `key` into a freshly allocated
/// buffer, writing its pointer/length to `out_ptr`/`out_len`. The
/// caller must release that buffer with [`mdc_lite_free_buffer`],
/// passing back the same length. On any non-Ok status, `*out_ptr` is
/// set to null and `*out_len` to 0.
///
/// # Safety
/// `store` must be a live pointer from [`mdc_lite_open`]. `key` must be
/// a valid null-terminated C string. `out_ptr`/`out_len` must each
/// point at valid, writable storage for one pointer/`usize`.
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_get(
    store: *mut LiteStore,
    key: *const c_char,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> MdcLiteStatus {
    if store.is_null() || key.is_null() || out_ptr.is_null() || out_len.is_null() {
        return MdcLiteStatus::InvalidArgument;
    }
    unsafe {
        *out_ptr = ptr::null_mut();
        *out_len = 0;
    }
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let store = unsafe { &*store };
        let key = match unsafe { CStr::from_ptr(key) }.to_str() {
            Ok(k) => k,
            Err(_) => return MdcLiteStatus::InvalidArgument,
        };
        match store.get(key) {
            Ok(mut value) => {
                value.shrink_to_fit();
                let len = value.len();
                let ptr = value.as_mut_ptr();
                std::mem::forget(value);
                unsafe {
                    *out_ptr = ptr;
                    *out_len = len;
                }
                MdcLiteStatus::Ok
            }
            Err(e) => e.into(),
        }
    }));
    result.unwrap_or(MdcLiteStatus::InternalError)
}

/// Releases a buffer returned by [`mdc_lite_get`].
///
/// # Safety
/// `buf`/`len` must be exactly the pointer/length pair returned by a
/// prior [`mdc_lite_get`] call, not yet freed.
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_free_buffer(buf: *mut u8, len: usize) {
    if buf.is_null() {
        return;
    }
    let _ = panic::catch_unwind(AssertUnwindSafe(|| unsafe {
        drop(Vec::from_raw_parts(buf, len, len));
    }));
}

/// # Safety
/// `store` must be a live pointer from [`mdc_lite_open`]. `key` must be
/// a valid null-terminated C string.
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_exists(store: *mut LiteStore, key: *const c_char) -> i32 {
    if store.is_null() || key.is_null() {
        return 0;
    }
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let store = unsafe { &*store };
        let key = match unsafe { CStr::from_ptr(key) }.to_str() {
            Ok(k) => k,
            Err(_) => return false,
        };
        store.exists(key)
    }));
    if result.unwrap_or(false) {
        1
    } else {
        0
    }
}

/// # Safety
/// `store` must be a live pointer from [`mdc_lite_open`]. `key` must be
/// a valid null-terminated C string.
#[no_mangle]
pub unsafe extern "C" fn mdc_lite_delete(store: *mut LiteStore, key: *const c_char) -> MdcLiteStatus {
    if store.is_null() || key.is_null() {
        return MdcLiteStatus::InvalidArgument;
    }
    let result = panic::catch_unwind(AssertUnwindSafe(|| {
        let store = unsafe { &*store };
        let key = match unsafe { CStr::from_ptr(key) }.to_str() {
            Ok(k) => k,
            Err(_) => return MdcLiteStatus::InvalidArgument,
        };
        match store.delete(key) {
            Ok(()) => MdcLiteStatus::Ok,
            Err(e) => e.into(),
        }
    }));
    result.unwrap_or(MdcLiteStatus::InternalError)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    #[test]
    fn ffi_round_trip() {
        unsafe {
            let dir = tempfile::tempdir().unwrap();
            let dir_c = CString::new(dir.path().to_str().unwrap()).unwrap();
            let key = [7u8; KEY_LEN];

            let store = mdc_lite_open(dir_c.as_ptr(), key.as_ptr());
            assert!(!store.is_null());

            let key_c = CString::new("k").unwrap();
            let value = b"hello ffi";
            let status = mdc_lite_put(store, key_c.as_ptr(), value.as_ptr(), value.len());
            assert_eq!(status, MdcLiteStatus::Ok);

            let mut out_ptr: *mut u8 = ptr::null_mut();
            let mut out_len: usize = 0;
            let status = mdc_lite_get(store, key_c.as_ptr(), &mut out_ptr, &mut out_len);
            assert_eq!(status, MdcLiteStatus::Ok);
            let got = slice::from_raw_parts(out_ptr, out_len);
            assert_eq!(got, value);
            mdc_lite_free_buffer(out_ptr, out_len);

            assert_eq!(mdc_lite_exists(store, key_c.as_ptr()), 1);
            assert_eq!(mdc_lite_delete(store, key_c.as_ptr()), MdcLiteStatus::Ok);
            assert_eq!(mdc_lite_exists(store, key_c.as_ptr()), 0);

            mdc_lite_close(store);
        }
    }

    #[test]
    fn ffi_get_missing_key_reports_not_found_and_nulls_output() {
        unsafe {
            let dir = tempfile::tempdir().unwrap();
            let dir_c = CString::new(dir.path().to_str().unwrap()).unwrap();
            let key = [1u8; KEY_LEN];
            let store = mdc_lite_open(dir_c.as_ptr(), key.as_ptr());

            let key_c = CString::new("nope").unwrap();
            let mut out_ptr: *mut u8 = ptr::null_mut();
            let mut out_len: usize = 123;
            let status = mdc_lite_get(store, key_c.as_ptr(), &mut out_ptr, &mut out_len);
            assert_eq!(status, MdcLiteStatus::NotFound);
            assert!(out_ptr.is_null());
            assert_eq!(out_len, 0);

            mdc_lite_close(store);
        }
    }

    #[test]
    fn ffi_null_pointers_return_invalid_argument_not_a_crash() {
        unsafe {
            assert!(mdc_lite_open(ptr::null(), ptr::null()).is_null());
            assert_eq!(mdc_lite_put(ptr::null_mut(), ptr::null(), ptr::null(), 0), MdcLiteStatus::InvalidArgument);
            let mut out_ptr: *mut u8 = ptr::null_mut();
            let mut out_len: usize = 0;
            assert_eq!(mdc_lite_get(ptr::null_mut(), ptr::null(), &mut out_ptr, &mut out_len), MdcLiteStatus::InvalidArgument);
            assert_eq!(mdc_lite_exists(ptr::null_mut(), ptr::null()), 0);
            assert_eq!(mdc_lite_delete(ptr::null_mut(), ptr::null()), MdcLiteStatus::InvalidArgument);
            mdc_lite_close(ptr::null_mut()); // must not crash
            mdc_lite_free_buffer(ptr::null_mut(), 0); // must not crash
        }
    }
}
