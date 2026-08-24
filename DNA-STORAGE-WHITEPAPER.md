# DNA-Inspired Storage, and Where Quantum Computing Actually Fits

**Version 1.0 | August 2026 | Research prototype, not a production claim**

---

## Abstract

This repository contains three independent, real implementations of a "DNA-inspired" idea: [MDC](mdc/)'s `dna/` module, which encodes arbitrary bytes into simulated DNA base sequences (A/C/G/T) with a working error-correction and corruption-simulation harness; [mdc-lite](mdc-lite/), whose on-disk format now DNA-encodes every entry the same way (`mdc-lite/src/dna.rs`), so both products share one storage model rather than two similar-but-different ones; and [MemCell](MEMCELL.md), a compression engine whose pipeline stages are explicitly modeled on biological information storage (codon tables, chromosome-style byte-plane sorting, synaptic-style pattern reuse). None perform physical DNA synthesis or sequencing; all three are software encodings that borrow DNA's *representational* ideas.

This document explains that encoding honestly, and then addresses a question that keeps coming up in the same breath as "DNA storage": **what does quantum computing have to do with any of this?** The honest answer is: nothing about the encoding itself, but something real and specific about the *cryptography* protecting data meant to survive as long as DNA storage's own value proposition claims it can. We explain exactly where that connection is real, and correct the more common version of the question - "is this quantum encryption?" - which conflates two unrelated technologies.

---

## Part 1: The DNA-inspired encoding that actually exists

### 1.1 The encoding itself

[`mdc/src/mdc/dna/encoder.py`](mdc/src/mdc/dna/encoder.py) implements a direct, reversible mapping from binary to DNA bases, 2 bits per base:

```
00 -> A      01 -> C      10 -> G      11 -> T
```

Every byte becomes exactly 4 bases. This is the same information-density idea DNA storage research is built on: 2 bits per symbol, 4 symbols per byte, no padding ambiguity on decode. It is a **simulation** - a string of `ACGT` characters in a database (MDC) or a file (mdc-lite), not a molecule. No physical synthesis, no sequencing, no wet lab anywhere in this repository.

Both implementations apply this mapping to *ciphertext*, never plaintext: the mapping above is public - reading this document teaches it to anyone - so encoding plaintext directly would only be obfuscation, easily reversed by anyone who's read this far. Encryption (AES-256-GCM in MDC's `dna/` module, XChaCha20-Poly1305 in mdc-lite) is applied first; the DNA encoding wraps the resulting ciphertext. That ordering is what makes a stored entry actually unreadable without the key, not the base-letter representation itself.

### 1.2 Redundancy the way DNA storage actually needs it

[`mdc/src/mdc/dna/ecc.py`](mdc/src/mdc/dna/ecc.py) implements one concrete error-correction scheme: N-way repetition with byte-level majority voting across independently corrupted copies. This is deliberately modeled on how physical DNA storage redundancy works in practice - separate strands, separately read, not one strand with inline parity - which sidesteps the alignment problem an insertion or deletion would cause in a single concatenated blob (a shifted copy can't be compared byte-for-byte against an unshifted one). Reed-Solomon, fountain codes, and LDPC are named as future options in this project's own spec but are not implemented; this document does not claim otherwise.

### 1.3 A corruption simulator, not measured hardware data

[`mdc/src/mdc/dna/corruption.py`](mdc/src/mdc/dna/corruption.py) applies configurable, seeded probabilities for the four error classes real DNA sequencing exhibits - substitution, insertion, deletion, and whole-read dropout - so the ECC scheme above can be tested against them reproducibly. These are *shapes* of real error classes, not statistics measured from any real sequencing platform. Treat every number this simulator produces as synthetic.

### 1.4 Why archival, not hot storage

CLAUDE.md's own design principle (section 43) is explicit: DNA-style encoding is scoped to the `ARCHIVE` storage tier, reached only by explicit request, never inferred from low access frequency alone. Nothing in this repository claims DNA-style encoding is fast, cheap to update, or a replacement for conventional hot/warm storage. The actual research question - the one real DNA storage papers investigate - is density and multi-decade durability for data that is rarely, if ever, read back. That framing matters directly for Part 3.

### 1.5 MemCell's own bio-inspired lineage

MemCell's pipeline was designed independently of MDC's `dna/` module but reaches for the same biological vocabulary on its own terms - its [whitepaper](WHITEPAPER.md) names a "pattern genome codebook" modeled on DNA codon tables, byte-plane transposition modeled on chromosome sorting, and delta encoding modeled on neural adaptation. It is a compression engine, not a storage-encoding scheme - it does not produce ACGT sequences - but the biological framing is genuinely load-bearing in how its pipeline is organized, not decorative naming applied after the fact.

---

## Part 2: Where quantum computing actually connects to this - and where it doesn't

### 2.1 What quantum computing threatens, precisely

Quantum computers threaten cryptography through two specific algorithms, and their impact is not uniform:

- **Shor's algorithm** breaks the *asymmetric* cryptography (RSA, elliptic-curve/ECC) that underlies most key exchange and digital signatures in use today, given a sufficiently large fault-tolerant quantum computer - a real but not-yet-existing threat.
- **Grover's algorithm** provides a quadratic speedup against *symmetric* ciphers (AES, ChaCha20) and hashes, which in practice only **halves** the effective key strength. A 256-bit symmetric key degrades to roughly 128-bit *quantum* security - still enormous, still considered secure for the foreseeable future by NIST and every major cryptography body.

This is the distinction that matters for everything below: **quantum computing is a much bigger problem for asymmetric cryptography than for symmetric cryptography.**

### 2.2 "Quantum encryption" is a different, unrelated technology

Quantum key distribution (QKD) - photons transmitted over dedicated fiber or free-space links between two fixed physical endpoints, using specialized hardware - is sometimes called "quantum encryption." It is a real technology, used today for a handful of point-to-point links (bank-to-bank, some government links). It is **not** relevant to data at rest, cannot run inside an application, and has no bearing on DNA-inspired storage, mdc-lite, or anything else in this repository. Nothing here uses QKD, and nothing here should be described as "quantum encryption."

### 2.3 The real connection: "harvest now, decrypt later"

Here is the genuine, specific link between DNA-inspired archival storage and quantum computing, and it has nothing to do with the encoding scheme itself:

DNA storage's entire value proposition - the reason the research field exists at all - is **multi-decade to multi-century retention** at high density. If data encrypted today with an asymmetric algorithm (RSA/ECC key exchange, for instance) is archived for 50-100 years, an adversary can record the ciphertext now and decrypt it later, once a sufficiently powerful quantum computer exists to run Shor's algorithm against it. This is a well-documented concern in the security community, usually called **"harvest now, decrypt later,"** and it applies to *any* long-retention archive using asymmetric cryptography - DNA-encoded or not. DNA storage's own multi-decade design horizon simply makes the concern more acute than it is for data that's re-encrypted or naturally expires on a five-year cycle.

The practical implication for a real DNA-archival system, if it ever needs asymmetric cryptography (key exchange between parties, digital signatures for provenance) is straightforward: use **post-quantum cryptography (PQC)** from the start, specifically the algorithms NIST has standardized - **ML-KEM** (formerly Kyber) for key encapsulation and **ML-DSA** (formerly Dilithium) for signatures - rather than RSA/ECC, since data written today may still need to be confidential decades from now.

**What this repository actually does today:** nothing that needs this. MDC's DNA-encoding tier (AES-256-GCM) and mdc-lite's local encrypted store (XChaCha20-Poly1305) both use only **symmetric** cryptography - no asymmetric key exchange happens anywhere in either project. Per §2.1, that means the quantum threat model here is already close to a non-issue: a 256-bit symmetric key stays secure under Grover's algorithm. Post-quantum asymmetric cryptography is a **forward-looking design recommendation** for if/when this project ever adds remote key exchange or multi-party archival provenance (a genuinely plausible future direction for a real DNA-archival product) - not a gap in what exists now, because nothing here currently has the asymmetric-crypto attack surface PQC exists to protect.

### 2.4 Crypto-agility as the actual design principle

The concrete, buildable takeaway for a system with DNA storage's retention horizon isn't "add quantum encryption" - it's **crypto-agility**: don't hard-code a single cryptographic algorithm into a format meant to outlive the algorithm's own security assumptions. Store an explicit algorithm identifier alongside every encrypted block (mdc-lite's on-disk format already does this implicitly - version the format, not just the bytes) so a future migration to a stronger algorithm doesn't require reinterpreting decades-old ciphertext blind. This is the same principle CLAUDE.md section 38 already states for error-correction ("the storage engine must not depend on a particular ECC algorithm") applied to cryptography instead.

---

## Summary

| Claim | Status |
|---|---|
| Binary-to-DNA-base encoding (2 bits/base, A/C/G/T) | **Real, implemented** in both products - [`mdc/dna/encoder.py`](mdc/src/mdc/dna/encoder.py) and [`mdc-lite/src/dna.rs`](mdc-lite/src/dna.rs) |
| Encryption applied before DNA encoding, both products | **Real, implemented** - AES-256-GCM (MDC) / XChaCha20-Poly1305 (mdc-lite); the encoding wraps ciphertext, never plaintext |
| N-way repetition ECC with majority voting | **Real, implemented**, [`dna/ecc.py`](mdc/src/mdc/dna/ecc.py) |
| Corruption simulation (substitution/insertion/deletion/dropout) | **Real, implemented, synthetic data**, [`dna/corruption.py`](mdc/src/mdc/dna/corruption.py) |
| Physical DNA synthesis or sequencing | **Does not exist in this repository** |
| Bearer-token authentication on MDC's REST API | **Real, implemented** - [`security/tokens.py`](mdc/src/mdc/security/tokens.py), issued via `mdc token issue` |
| "Quantum encryption" (QKD) | **Not used, not applicable to local/archival storage** |
| Quantum computers threaten this project's symmetric crypto today | **No** - Grover's algorithm leaves 256-bit keys secure |
| Post-quantum asymmetric crypto (ML-KEM/ML-DSA) | **Not needed today** (no asymmetric crypto exists here yet); a real, honest recommendation *if* remote key exchange is ever added |
| DNA storage's real research value | Density + archival durability - an active research hypothesis, not an established product claim (CLAUDE.md section 55) |
| Windows CLI bridge into an mdc-lite store; stronger DNA-tier ECC | **Phase 2, not yet built** - roadmap items for MDC Platform's storage side |
