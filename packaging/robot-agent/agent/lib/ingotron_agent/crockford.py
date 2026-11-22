"""Crockford base32, prefix form -- the CA-pin encoder.

The pin is the FIRST 26 CHARACTERS OF THE ENCODING OF THE WHOLE SHA-256(SPKI)
digest.  It is NOT the encoding of the digest's first 16 bytes; those are
different strings, because 26 Crockford characters carry 130 bits and the 26th
character mixes in two bits of the 17th byte, which a 16-byte encoding would
zero-pad away.

There are three implementations of this function in the product -- the POSIX-sh
verifier in packaging/robot-agent/robot.sh, the Rust minter in
phases/03-ingest-server-rs/src/crockford.rs, and this one -- and they are held together by ONE fixture
set, not by a claim of shared code:

    packaging/robot-agent/tests/fixtures/b32_prefix_vectors.tsv

A drift between minter and verifier is a total enrollment outage.  If you
change this file, re-run that fixture against all three.
"""

from typing import Optional

#: Crockford's alphabet: no I, no L, no O, no U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: The pin segment of an `ign1.` code.
CA_PIN_CHARS = 26


def b32_prefix(hex_digest: str, want: int) -> Optional[str]:
    """Return the first `want` Crockford-base32 characters of `bytes.fromhex(hex_digest)`.

    Returns None -- never a partial or padded answer -- when the input is empty,
    is not hexadecimal, or is too short to produce `want` characters.  That is
    the same failure contract as the shell function's `return 1`.
    """
    if want <= 0:
        return None
    acc = 0
    bits = 0
    out = []
    for ch in hex_digest:
        try:
            value = int(ch, 16)
        except ValueError:
            return None
        acc = (acc << 4) | value
        bits += 4
        while bits >= 5 and len(out) < want:
            index = (acc >> (bits - 5)) & 31
            bits -= 5
            acc &= (1 << bits) - 1
            out.append(ALPHABET[index])
        if len(out) >= want:
            break
    if len(out) != want:
        return None
    return "".join(out)


def ca_pin_from_spki_digest_hex(hex_digest: str) -> Optional[str]:
    """The 26-character CA pin from the hex SHA-256 of the CA's SPKI."""
    return b32_prefix(hex_digest, CA_PIN_CHARS)
