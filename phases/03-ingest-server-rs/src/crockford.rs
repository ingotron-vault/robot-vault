//! Crockford base32 — the one encoder the enrollment code's CA pin is minted
//! with and the one the robot bootstrapper verifies it with.
//!
//! The pin is **the first 26 characters of the Crockford-base32 encoding of the
//! whole `SHA-256(SPKI)`**, not the encoding of the digest's first 16 bytes.
//! Those are different strings: 26 characters carry 130 bits, so the 26th
//! character mixes in two bits of the 17th byte, which a 16-byte encoding would
//! zero-pad away.
//!
//! A mismatch between the minter here and the `b32_prefix` shell function in
//! `/robot` is a total enrollment outage, so `b32_prefix_hex` below is a
//! deliberately literal transcription of the POSIX function — same nibble-at-a-
//! time accumulator, same "stop consuming once `want` characters are emitted"
//! loop condition, same failure on empty/short/non-hex input — and the shared
//! fixture vectors
//! (`packaging/robot-agent/tests/fixtures/b32_prefix_vectors.tsv`, executed
//! under `dash`, `busybox ash` and an independent Python implementation) are asserted against it in this file's tests.

/// Crockford's alphabet: no `I`, `L`, `O` or `U`.
pub(crate) const CROCKFORD_ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// Number of Crockford characters in the enrollment code's CA-pin segment.
pub(crate) const CA_PIN_CHARS: usize = 26;

/// Encode `bytes` as unpadded Crockford base32, five bits at a time, MSB
/// first. A trailing partial group is zero-extended on the right, which is what
/// makes `encode(d)[..26]` equal `b32_prefix_hex(hex(d), 26)` for any digest
/// long enough to fill 26 characters (asserted below).
pub(crate) fn encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity((bytes.len() * 8).div_ceil(5));
    let mut acc: u16 = 0;
    let mut bits: u32 = 0;
    for &byte in bytes {
        acc = (acc << 8) | u16::from(byte);
        bits += 8;
        while bits >= 5 {
            let index = ((acc >> (bits - 5)) & 31) as usize;
            bits -= 5;
            acc &= (1u16 << bits) - 1;
            out.push(CROCKFORD_ALPHABET[index] as char);
        }
    }
    if bits > 0 {
        let index = ((acc << (5 - bits)) & 31) as usize;
        out.push(CROCKFORD_ALPHABET[index] as char);
    }
    out
}

/// The Rust twin of the bootstrapper's `b32_prefix`: hex in, the first
/// `want` Crockford characters of the bytes it denotes out.
///
/// `None` means the shell function's `return 1`: non-hex input, or not enough
/// input to produce `want` characters. Like the shell, hex characters after the
/// `want`-th emitted character are never examined, so validation of the tail is
/// deliberately *not* stronger here than it is there.
pub(crate) fn b32_prefix_hex(hex: &str, want: usize) -> Option<String> {
    let mut out = String::with_capacity(want);
    let mut acc: u32 = 0;
    let mut bits: u32 = 0;
    for character in hex.chars() {
        if out.len() >= want {
            break;
        }
        let value = character.to_digit(16)?;
        acc = (acc << 4) | value;
        bits += 4;
        while bits >= 5 && out.len() < want {
            let index = ((acc >> (bits - 5)) & 31) as usize;
            bits -= 5;
            acc &= (1u32 << bits) - 1;
            out.push(CROCKFORD_ALPHABET[index] as char);
        }
    }
    if out.len() == want {
        Some(out)
    } else {
        None
    }
}

/// Mint the enrollment-code CA pin from the SHA-256 of the ingest CA's
/// SubjectPublicKeyInfo.
///
/// Routed through `b32_prefix_hex` rather than through `encode` on purpose: the
/// minter and the shell verifier then execute the *same* algorithm rather than
/// two that happen to agree today.
pub(crate) fn ca_pin_from_spki_digest(digest: &[u8]) -> Option<String> {
    b32_prefix_hex(&hex::encode(digest), CA_PIN_CHARS)
}

/// True when every character is in the Crockford alphabet exactly as minted.
///
/// Deliberately strict — no case folding and no `I`/`L`→`1`, `O`→`0` transcription
/// repair. The code is copy-pasted as part of a command and never
/// typed, and accepting two spellings of one code would mean two SHA-256 values
/// for one database row.
pub(crate) fn is_canonical_crockford(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| CROCKFORD_ALPHABET.contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The shared fixture set.
    /// These six rows are the shared contract between this encoder, `/robot`'s
    /// `b32_prefix` under `dash` and `busybox ash`, and the independent Python
    /// cross-check. Changing an expected value here without changing all three
    /// is the enrollment outage this fixture exists to prevent.
    const SHARED_FIXTURE_VECTORS: &[(&str, usize, Option<&str>)] = &[
        (
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
            26,
            Some("5NRHCGNQ4TR480B2FJMZQB1JYQ"),
        ),
        (
            "0000000000000000000000000000000000",
            26,
            Some("00000000000000000000000000"),
        ),
        (
            "ffffffffffffffffffffffffffffffffff",
            26,
            Some("ZZZZZZZZZZZZZZZZZZZZZZZZZZ"),
        ),
        ("", 26, None),
        ("abcd", 26, None),
        ("zz", 4, None),
    ];

    #[test]
    fn b32_prefix_matches_the_shared_fixture_vectors() {
        for (input, want, expected) in SHARED_FIXTURE_VECTORS {
            assert_eq!(
                b32_prefix_hex(input, *want).as_deref(),
                *expected,
                "fixture vector {input:?} (want {want}) diverged from the shell encoder"
            );
        }
    }

    #[test]
    fn the_first_vector_is_really_the_sha256_of_x() {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        hasher.update(b"x");
        let digest = hasher.finalize();
        assert_eq!(
            hex::encode(digest),
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
        );
        // The minter's own entry point must produce the fixture's pin, not just
        // the low-level helper.
        assert_eq!(
            ca_pin_from_spki_digest(&digest).unwrap(),
            "5NRHCGNQ4TR480B2FJMZQB1JYQ"
        );
    }

    #[test]
    fn the_minter_and_the_streaming_encoder_agree_on_the_pin_prefix() {
        // 130 bits of the digest, encoded twice by two different routines.
        for seed in 0u8..64 {
            let digest: Vec<u8> = (0..32u8)
                .map(|i| i.wrapping_mul(31).wrapping_add(seed))
                .collect();
            let streamed = encode(&digest);
            let prefixed = ca_pin_from_spki_digest(&digest).unwrap();
            assert_eq!(&streamed[..CA_PIN_CHARS], prefixed.as_str());
        }
    }

    #[test]
    fn the_pin_is_not_the_encoding_of_the_first_sixteen_bytes() {
        // The 26-character rule, asserted rather than described: truncating the
        // input and truncating the output are different strings, so an
        // implementation that encodes only the first 16 bytes fails here.
        let digest =
            hex::decode("2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")
                .unwrap();
        let whole_digest_pin = ca_pin_from_spki_digest(&digest).unwrap();
        let first_sixteen_bytes = encode(&digest[..16]);
        assert_eq!(&whole_digest_pin[..25], &first_sixteen_bytes[..25]);
        assert_ne!(whole_digest_pin, first_sixteen_bytes[..26].to_string());
    }

    #[test]
    fn encode_is_unpadded_and_sized_by_bit_count() {
        // 20 bytes = 160 bits = exactly 32 characters (the code secret).
        assert_eq!(encode(&[0u8; 20]).len(), 32);
        // 32 bytes = 256 bits = 52 characters (the robot credential secret).
        assert_eq!(encode(&[0u8; 32]).len(), 52);
        assert_eq!(encode(&[0xff; 20]), "Z".repeat(32));
    }

    #[test]
    fn canonical_crockford_rejects_the_excluded_letters_and_lower_case() {
        assert!(is_canonical_crockford("5NRHCGNQ4TR480B2FJMZQB1JYQ"));
        assert!(!is_canonical_crockford(""));
        assert!(!is_canonical_crockford("5nrhcgnq4tr480b2fjmzqb1jyq"));
        for excluded in ["I", "L", "O", "U", "-", "."] {
            assert!(
                !is_canonical_crockford(excluded),
                "{excluded} must not be accepted"
            );
        }
    }

    #[test]
    fn b32_prefix_stops_reading_once_the_prefix_is_complete() {
        // Exactly the shell's loop condition: garbage past the 130th bit is
        // never inspected. Asserted so a "stricter" rewrite that validates the
        // whole string is caught as the divergence it would be.
        let mut hex =
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881".to_string();
        hex.push_str("zzzz");
        assert_eq!(
            b32_prefix_hex(&hex, 26).as_deref(),
            Some("5NRHCGNQ4TR480B2FJMZQB1JYQ")
        );
    }
}
