//! Binary <-> DNA base encoding, the same 2-bit-per-base mapping MDC
//! Platform's `dna/encoder.py` uses (`00->A 01->C 10->G 11->T`) - both
//! products store data the same way, matching this crate's own DNA
//! branding rather than just alluding to it.
//!
//! This is applied to the *encrypted* file bytes in `lib.rs`, never to
//! plaintext - the mapping itself is public (anyone reading this file
//! learns it), so encoding plaintext directly would only be
//! obfuscation, not protection. Every byte maps to exactly 4 bases (no
//! padding ambiguity to resolve on decode), so `decode` only needs to
//! reject a length that isn't a multiple of 4 or a symbol outside
//! A/C/G/T.

const BASES: [u8; 4] = [b'A', b'C', b'G', b'T'];

pub fn encode(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len() * 4);
    for &byte in data {
        for shift in [6, 4, 2, 0] {
            let bits = (byte >> shift) & 0b11;
            out.push(BASES[bits as usize]);
        }
    }
    out
}

pub fn decode(sequence: &[u8]) -> Option<Vec<u8>> {
    if sequence.len() % 4 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(sequence.len() / 4);
    for chunk in sequence.chunks_exact(4) {
        let mut byte = 0u8;
        for &base in chunk {
            let bits = match base {
                b'A' => 0b00,
                b'C' => 0b01,
                b'G' => 0b10,
                b'T' => 0b11,
                _ => return None,
            };
            byte = (byte << 2) | bits;
        }
        out.push(byte);
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_every_byte_value() {
        let data: Vec<u8> = (0..=255u8).collect();
        assert_eq!(decode(&encode(&data)).unwrap(), data);
    }

    #[test]
    fn empty_round_trips() {
        assert_eq!(decode(&encode(&[])).unwrap(), Vec::<u8>::new());
    }

    #[test]
    fn encoded_output_uses_only_acgt() {
        let encoded = encode(&(0..=255u8).collect::<Vec<u8>>());
        assert!(encoded.iter().all(|b| matches!(b, b'A' | b'C' | b'G' | b'T')));
    }

    #[test]
    fn four_bases_per_byte() {
        assert_eq!(encode(b"twelve bytes!").len(), 13 * 4);
    }

    #[test]
    fn decode_rejects_length_not_a_multiple_of_four() {
        assert!(decode(b"ACG").is_none());
    }

    #[test]
    fn decode_rejects_non_acgt_symbols() {
        assert!(decode(b"ACGX").is_none());
    }
}
