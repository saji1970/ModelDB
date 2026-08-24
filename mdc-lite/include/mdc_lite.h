/*
 * mdc-lite: C ABI header for the Swift/Kotlin/C bridge.
 *
 * This mirrors src/ffi.rs exactly - if you change one, change the
 * other. Not auto-generated (the FFI surface is small enough that a
 * hand-written header stays easy to keep in sync; reach for cbindgen
 * if this grows).
 */
#ifndef MDC_LITE_H
#define MDC_LITE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MDC_LITE_KEY_LEN 32

typedef enum {
    MDC_LITE_OK = 0,
    MDC_LITE_NOT_FOUND = 1,
    MDC_LITE_CRYPTO_ERROR = 2,
    MDC_LITE_IO_ERROR = 3,
    MDC_LITE_INVALID_ARGUMENT = 4,
    MDC_LITE_INTERNAL_ERROR = 5,
} MdcLiteStatus;

typedef struct MdcLiteStore MdcLiteStore;

/*
 * Opens (creating if needed) a store at dir_path using the 32 bytes at
 * key_ptr as the store's encryption key. key_ptr must point at exactly
 * MDC_LITE_KEY_LEN bytes - obtain that key from the platform's secure
 * storage (Keychain/Secure Enclave, Keystore/StrongBox); this library
 * never generates or persists it itself. Returns NULL on failure.
 */
MdcLiteStore *mdc_lite_open(const char *dir_path, const uint8_t *key_ptr);

/* Releases a handle from mdc_lite_open. Safe to call with NULL. */
void mdc_lite_close(MdcLiteStore *store);

/* Encrypts and stores value_ptr[0..value_len) under key. */
MdcLiteStatus mdc_lite_put(MdcLiteStore *store, const char *key, const uint8_t *value_ptr, size_t value_len);

/*
 * Decrypts the value stored under key into a freshly allocated buffer.
 * On MDC_LITE_OK, *out_ptr/*out_len describe that buffer and the
 * caller must release it with mdc_lite_free_buffer. On any other
 * status, *out_ptr is set to NULL and *out_len to 0.
 */
MdcLiteStatus mdc_lite_get(MdcLiteStore *store, const char *key, uint8_t **out_ptr, size_t *out_len);

/* Releases a buffer returned by mdc_lite_get. */
void mdc_lite_free_buffer(uint8_t *buf, size_t len);

/* Returns 1 if key exists, 0 otherwise (including on any error). */
int32_t mdc_lite_exists(MdcLiteStore *store, const char *key);

/* Deletes key. Idempotent - deleting a key that doesn't exist is MDC_LITE_OK. */
MdcLiteStatus mdc_lite_delete(MdcLiteStore *store, const char *key);

#ifdef __cplusplus
}
#endif

#endif /* MDC_LITE_H */
