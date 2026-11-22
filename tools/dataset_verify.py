#!/usr/bin/env python3
"""Offline verifier for Robot Vault dataset manifests (Pro publishes them).

    python tools/dataset_verify.py manifest.json \
        --signature <hex> --pubkey dataset_signing.pem [--check-files DIR]

Recomputes `dataset_id` from the manifest, verifies the Ed25519 signature
over the same canonical bytes, and -- with `--check-files` -- re-hashes the
media files and parquet objects the manifest lists against a local copy of
the export/bucket tree. Prints a PASS/FAIL verdict and exits non-zero on any
failure.

MANIFEST SCHEMA
===============
This tool reads one manifest schema, `robot-vault-dataset-manifest/v2`.
Every media entry and every parquet entry carries `sha256`: 64 hexadecimal
characters that the writer recorded as it wrote the object. `--check-files`
re-hashes each object and compares it with that digest.

The schema string is part of the canonical bytes, but the canonicalization
algorithm itself does not interpret it.  This verifier does: an unknown
schema is rejected rather than guessed at, because a future schema may list
artifacts in places this version does not know to inspect.

`--check-files` is deliberately all-or-nothing.  Every media and parquet
artifact listed by the manifest must exist below the selected verification
root and match its recorded sha256.  Absolute paths,
`.`/`..` components, backslashes, and symlink escapes are rejected.  This
prevents an untrusted manifest from making the verifier read elsewhere on the
machine running it.

WHY THIS FILE IMPORTS NOTHING FROM THIS PROJECT
===============================================
This is the artifact a recipient runs on *their* machine, months later,
without access to the vendor. It depends on the Python standard library plus
`cryptography` (`pip install cryptography`) and nothing else -- no
`query_service`, no `robot_vault_config`, no repo checkout, no container. Copy
this single file next to a manifest and it works.

That independence has a price and the price is the point: the canonical-JSON
rules below are a **re-implementation** of the published contract, not an
import of the service's code. If the two ever drift, `dataset_id` computed
here stops matching the one the service issued -- which is precisely the
failure a recipient needs to be able to detect.

THE CANONICALIZATION CONTRACT (`robot-vault-canonical-json/v1`)
============================================================
 1. UTF-8, no BOM, no trailing newline.
 2. Object keys sorted ascending by Unicode code point, at every depth.
 3. Separators exactly `,` and `:` -- no other whitespace outside strings.
 4. Only null / bool / int / float / str / list / dict may appear.
 5. Object keys must be strings.
 6. bool is `true`/`false`, never a number (it is an int subclass in Python).
 7. Integers: bare decimal digits, arbitrary precision, no exponent. NOTE:
    nanosecond timestamps exceed 2^53 -- parse with a big-int-safe reader
    (Python's `json` is; JavaScript's `JSON.parse` is NOT).
 8. Floats must be finite and use shortest-round-trip decimal.
 9. Any key ending in `_ns` carries an int or null, never a float.
10. Strings keep non-ASCII characters literal; only `"`, `\\` and C0 control
    characters are escaped, with lowercase `\\u00xx` for the ones without a
    short escape.
11. No cycles.

`dataset_id = sha256(canonical_bytes(manifest))`, lowercase hex.
The Ed25519 signature is over those same canonical bytes.

EXIT CODES
==========
  0  every requested check passed
  1  a check failed (id mismatch, bad signature, file hash mismatch, ...)
  2  usage / input error (bad path, unreadable manifest, missing dependency)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Set, Tuple

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

CANONICALIZATION_SCHEMA = "robot-vault-canonical-json/v1"
#: The only manifest schema this tool reads.  Other schemas fail closed:
#: their canonical bytes could be hashed, but this version cannot know which
#: nested artifacts another schema requires it to verify.
MANIFEST_SCHEMA = "robot-vault-dataset-manifest/v2"

_CHUNK = 1024 * 1024


class CanonicalizationError(ValueError):
    """The manifest contains something the canonical-JSON contract forbids."""


class ManifestValidationError(ValueError):
    """The JSON value does not conform to a supported manifest schema."""


class ArtifactPathError(ValueError):
    """A manifest artifact path is unsafe or cannot be resolved safely."""


# ---------------------------------------------------------------------------
# canonical JSON -- deliberate re-implementation, see the module docstring
# ---------------------------------------------------------------------------

def _validate(value: Any, path: str, seen: Set[int]) -> None:
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CanonicalizationError(f"{path}: string is not UTF-8 encodable: {exc}")
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalizationError(f"{path}: non-finite float {value!r} (rule 8)")
        return
    if isinstance(value, dict):
        if id(value) in seen:
            raise CanonicalizationError(f"{path}: circular reference (rule 11)")
        seen.add(id(value))
        for key, sub in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: non-string object key {key!r} (rule 5)")
            sub_path = f"{path}.{key}"
            if key.endswith("_ns") and not (
                sub is None or (isinstance(sub, int) and not isinstance(sub, bool))
            ):
                raise CanonicalizationError(
                    f"{sub_path}: nanosecond field must be int or null, got "
                    f"{type(sub).__name__} (rule 9)"
                )
            _validate(sub, sub_path, seen)
        seen.discard(id(value))
        return
    if isinstance(value, list):
        if id(value) in seen:
            raise CanonicalizationError(f"{path}: circular reference (rule 11)")
        seen.add(id(value))
        for index, sub in enumerate(value):
            _validate(sub, f"{path}[{index}]", seen)
        seen.discard(id(value))
        return
    raise CanonicalizationError(
        f"{path}: {type(value).__name__} is not a JSON type (rule 4)"
    )


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8 canonical JSON bytes for `obj` -- rules 1-11 above."""
    _validate(obj, "$", set())
    text = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        check_circular=True,
    )
    return text.encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    """Lowercase hex sha256 of the canonical bytes -- i.e. `dataset_id`."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


# ---------------------------------------------------------------------------
# versioned manifest structure
# ---------------------------------------------------------------------------

_MANIFEST_FIELDS = {
    "schema", "name", "filter_spec", "parent_dataset_id", "created_at_utc", "episodes",
}
_EPISODE_FIELDS = {
    "episode_id", "upload_id", "robot_id", "t_start_ns", "t_end_ns",
    "n_messages", "task_label", "source_mcap_sha256", "media", "parquet",
}
_MEDIA_FIELDS = {"key", "sha256", "codec", "n_frames", "pii_status"}
_PARQUET_FIELDS = {
    "file_path", "row_count", "file_size_bytes", "schema_version", "sha256",
}
_HEX = frozenset("0123456789abcdefABCDEF")


def _expect_fields(value: Any, expected: Set[str], path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(
            f"{path}: expected an object, got {type(value).__name__}"
        )
    actual = set(value)
    missing = expected - actual
    unexpected = actual - expected
    if missing or unexpected:
        details: List[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            details.append(
                "unexpected " + ", ".join(sorted((repr(item) for item in unexpected)))
            )
        raise ManifestValidationError(f"{path}: " + "; ".join(details))
    return value


def _expect_string(value: Any, path: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        expected = "a non-empty string or null" if nullable else "a non-empty string"
        raise ManifestValidationError(f"{path}: expected {expected}")


def _expect_optional_string(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ManifestValidationError(f"{path}: expected a string or null")


def _expect_int(
    value: Any, path: str, *, nullable: bool = True, minimum: Optional[int] = None
) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        expected = "an integer or null" if nullable else "an integer"
        raise ManifestValidationError(f"{path}: expected {expected}")
    if minimum is not None and value < minimum:
        raise ManifestValidationError(f"{path}: must be >= {minimum}")


def _expect_sha256(value: Any, path: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or len(value) != 64 or any(ch not in _HEX for ch in value):
        expected = "64 hexadecimal characters or null" if nullable else "64 hexadecimal characters"
        raise ManifestValidationError(f"{path}: expected {expected}")


def _artifact_parts(key: Any, path: str) -> List[str]:
    """Validate a manifest key and return its portable path components."""
    _expect_string(key, path)
    assert isinstance(key, str)  # narrowed by _expect_string
    if "\x00" in key:
        raise ManifestValidationError(f"{path}: NUL bytes are not allowed")
    if "\\" in key:
        raise ManifestValidationError(
            f"{path}: backslashes are not allowed; artifact keys use '/'"
        )
    posix = PurePosixPath(key)
    windows = PureWindowsPath(key)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise ManifestValidationError(f"{path}: absolute artifact paths are not allowed")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ManifestValidationError(
            f"{path}: empty, '.' and '..' path components are not allowed"
        )
    return parts


def validate_manifest(manifest: Any) -> Dict[str, Any]:
    """Validate the exact supported structure or raise a clear error."""
    manifest = _expect_fields(manifest, _MANIFEST_FIELDS, "$")
    schema = manifest["schema"]
    if schema != MANIFEST_SCHEMA:
        raise ManifestValidationError(
            f"$.schema: unsupported manifest schema {schema!r}; supported: {MANIFEST_SCHEMA}"
        )

    _expect_string(manifest["name"], "$.name")
    if not isinstance(manifest["filter_spec"], dict):
        raise ManifestValidationError("$.filter_spec: expected an object")
    _expect_string(manifest["parent_dataset_id"], "$.parent_dataset_id", nullable=True)
    _expect_string(manifest["created_at_utc"], "$.created_at_utc")
    episodes = manifest["episodes"]
    if not isinstance(episodes, list):
        raise ManifestValidationError(
            f"$.episodes: expected an array, got {type(episodes).__name__}"
        )

    for episode_index, episode_value in enumerate(episodes):
        episode_path = f"$.episodes[{episode_index}]"
        episode = _expect_fields(episode_value, _EPISODE_FIELDS, episode_path)
        for field in ("episode_id", "upload_id", "robot_id"):
            _expect_string(episode[field], f"{episode_path}.{field}")
        _expect_int(episode["t_start_ns"], f"{episode_path}.t_start_ns")
        _expect_int(episode["t_end_ns"], f"{episode_path}.t_end_ns")
        _expect_int(episode["n_messages"], f"{episode_path}.n_messages", minimum=0)
        _expect_optional_string(episode["task_label"], f"{episode_path}.task_label")
        _expect_sha256(
            episode["source_mcap_sha256"],
            f"{episode_path}.source_mcap_sha256",
            nullable=True,
        )

        media_entries = episode["media"]
        if not isinstance(media_entries, list):
            raise ManifestValidationError(f"{episode_path}.media: expected an array")
        for media_index, media_value in enumerate(media_entries):
            media_path = f"{episode_path}.media[{media_index}]"
            media = _expect_fields(media_value, _MEDIA_FIELDS, media_path)
            _artifact_parts(media["key"], f"{media_path}.key")
            _expect_sha256(media["sha256"], f"{media_path}.sha256")
            _expect_string(media["codec"], f"{media_path}.codec")
            _expect_int(media["n_frames"], f"{media_path}.n_frames", minimum=0)
            _expect_string(media["pii_status"], f"{media_path}.pii_status")

        parquet_entries = episode["parquet"]
        if not isinstance(parquet_entries, list):
            raise ManifestValidationError(f"{episode_path}.parquet: expected an array")
        for parquet_index, parquet_value in enumerate(parquet_entries):
            parquet_path = f"{episode_path}.parquet[{parquet_index}]"
            parquet = _expect_fields(parquet_value, _PARQUET_FIELDS, parquet_path)
            _artifact_parts(parquet["file_path"], f"{parquet_path}.file_path")
            _expect_int(
                parquet["row_count"], f"{parquet_path}.row_count",
                nullable=False, minimum=0,
            )
            _expect_int(
                parquet["file_size_bytes"],
                f"{parquet_path}.file_size_bytes",
                nullable=False,
                minimum=0,
            )
            _expect_int(
                parquet["schema_version"],
                f"{parquet_path}.schema_version",
                nullable=False,
                minimum=1,
            )
            _expect_sha256(parquet["sha256"], f"{parquet_path}.sha256")
    return manifest


# ---------------------------------------------------------------------------
# signature
# ---------------------------------------------------------------------------

def verify_signature(
    manifest_bytes: bytes, signature_hex: str, public_key_pem: bytes
) -> Tuple[bool, str]:
    """(ok, detail). Never raises: a malformed key/signature is a FAIL, not a crash."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - environment problem, not a data problem
        return False, "the `cryptography` package is not installed (pip install cryptography)"

    try:
        signature = bytes.fromhex(signature_hex.strip())
    except ValueError:
        return False, "signature is not valid hex"

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except Exception as exc:
        return False, f"public key is not a loadable PEM ({exc.__class__.__name__})"
    if not isinstance(public_key, Ed25519PublicKey):
        return False, f"public key is a {type(public_key).__name__}, not Ed25519"

    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature:
        return False, "Ed25519 signature does not match the manifest's canonical bytes"
    except Exception as exc:
        return False, f"Ed25519 signature could not be verified ({exc.__class__.__name__})"
    return True, "Ed25519 signature is valid over the manifest's canonical bytes"


# ---------------------------------------------------------------------------
# local file re-hashing
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(root: Path, key: str) -> Optional[Path]:
    """Find `key` below resolved `root`, tolerating rooted subtree layouts.

    A recipient may have synced the bucket verbatim (`root/processed/media/...`),
    pulled only the media subtree, or been handed a flat directory of chunks.
    Tried in decreasing order of specificity: `root/key`, then each suffix of
    `key`.  Arbitrary basename search is intentionally not used because it can
    bind an entry to an unrelated same-named file.

    Every existing candidate is resolved before use.  A symlink may point to
    another location inside `root`, but never outside it.
    """
    try:
        parts = _artifact_parts(key, "artifact path")
    except ManifestValidationError as exc:
        raise ArtifactPathError(str(exc)) from exc

    for drop in range(len(parts)):
        candidate = root.joinpath(*parts[drop:])
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            raise ArtifactPathError(
                f"{key}: cannot resolve {candidate} ({exc.__class__.__name__}: {exc})"
            ) from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ArtifactPathError(
                f"{key}: {candidate} resolves outside verification root {root}"
            ) from exc
        try:
            is_file = resolved.is_file()
        except OSError as exc:
            raise ArtifactPathError(
                f"{key}: cannot inspect {resolved} ({exc.__class__.__name__}: {exc})"
            ) from exc
        if not is_file:
            raise ArtifactPathError(f"{key}: {resolved} is not a regular file")
        return resolved
    return None


def check_files(manifest: Dict[str, Any], root: Path) -> Tuple[List[str], List[str], List[str]]:
    """Re-hash every file the manifest lists. -> (ok, failed, missing) lines."""
    ok: List[str] = []
    failed: List[str] = []
    missing: List[str] = []

    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return [], [f"cannot resolve verification root {root}: {exc}"], []

    # `--check-files` means the caller supplied the original lake tree, so
    # every listed media and parquet object must be present.  Checking only an
    # export bundle is a usage error: the manifest cites the original objects.
    for episode in manifest["episodes"]:
        artifacts = [(media["key"], media["sha256"]) for media in episode["media"]]
        artifacts += [
            (parquet["file_path"], parquet["sha256"]) for parquet in episode["parquet"]
        ]
        for key, expected in artifacts:
            try:
                path = _resolve(resolved_root, key)
            except ArtifactPathError as exc:
                failed.append(str(exc))
                continue
            if path is None:
                missing.append(f"{key}: not found under {root}")
                continue
            try:
                actual = sha256_file(path)
            except OSError as exc:
                failed.append(
                    f"{key}: cannot read {path} ({exc.__class__.__name__}: {exc})"
                )
                continue
            if actual == expected.lower():
                ok.append(f"{key}: sha256 matches ({expected[:16]}...)")
            else:
                failed.append(
                    f"{key}: sha256 MISMATCH -- manifest {expected}, local file {actual} ({path})"
                )
    return ok, failed, missing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _object_without_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _load_manifest(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    manifest = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dataset_verify.py",
        description=(
            "Offline verification of a Robot Vault dataset manifest: recompute "
            "dataset_id, verify the Ed25519 signature, optionally re-hash local files."
        ),
    )
    parser.add_argument("manifest", help="path to the manifest JSON")
    parser.add_argument("--signature", help="Ed25519 signature, hex (published with the dataset)")
    parser.add_argument("--pubkey", help="PEM public key file")
    parser.add_argument(
        "--expect-id",
        help="dataset_id you were told to expect; compared against the recomputed one",
    )
    parser.add_argument(
        "--check-files",
        metavar="DIR",
        help=(
            "re-hash the media files and parquet objects listed in the "
            "manifest against this local tree"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="print only the verdict")
    args = parser.parse_args(argv)

    def say(line: str = "") -> None:
        if not args.quiet:
            print(line)

    manifest_path = Path(args.manifest)
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, ValueError, RecursionError) as exc:
        print(f"FAIL: cannot read manifest {manifest_path}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    say(f"manifest:        {manifest_path}")
    schema = manifest.get("schema")
    say(f"schema:          {schema}")
    say(f"canonicalization: {CANONICALIZATION_SCHEMA}")
    say()

    try:
        validate_manifest(manifest)
    except (ManifestValidationError, RecursionError) as exc:
        print(f"FAIL: malformed or unsupported manifest: {exc}", file=sys.stderr)
        return EXIT_FAIL

    failures: List[str] = []
    checks: List[str] = []

    # --- dataset_id -------------------------------------------------------
    try:
        payload = canonical_bytes(manifest)
    except (CanonicalizationError, RecursionError) as exc:
        print(f"FAIL: manifest is not canonicalizable: {exc}", file=sys.stderr)
        return EXIT_FAIL
    recomputed = hashlib.sha256(payload).hexdigest()
    say(f"canonical bytes: {len(payload)}")
    say(f"dataset_id:      {recomputed}")
    if args.expect_id:
        if args.expect_id.strip().lower() == recomputed:
            checks.append("dataset_id matches the expected value")
        else:
            failures.append(
                f"dataset_id MISMATCH -- expected {args.expect_id.strip().lower()}, "
                f"recomputed {recomputed}"
            )
    else:
        checks.append("dataset_id recomputed from the manifest (nothing to compare it to; "
                      "pass --expect-id to assert)")

    # --- signature --------------------------------------------------------
    if args.signature and args.pubkey:
        try:
            pubkey_pem = Path(args.pubkey).read_bytes()
        except OSError as exc:
            print(f"FAIL: cannot read public key {args.pubkey}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        ok, detail = verify_signature(payload, args.signature, pubkey_pem)
        (checks if ok else failures).append(detail)
    elif args.signature or args.pubkey:
        failures.append(
            "--signature and --pubkey must be given together; cannot verify with only one"
        )
    else:
        say("signature:       NOT CHECKED -- no --signature/--pubkey supplied.")
        say("                 dataset_id still content-addresses the manifest, but")
        say("                 nothing here proves WHO issued it.")

    # --- local files ------------------------------------------------------
    if args.check_files:
        root = Path(args.check_files)
        if not root.is_dir():
            print(f"FAIL: --check-files {root} is not a directory", file=sys.stderr)
            return EXIT_USAGE
        ok_lines, failed_lines, missing_lines = check_files(manifest, root)
        say()
        say(f"file checks under {root}:")
        for line in ok_lines:
            say(f"  ok      {line}")
        for line in missing_lines:
            say(f"  missing {line}")
        for line in failed_lines:
            say(f"  FAIL    {line}")
        if failed_lines or missing_lines:
            failures.append(
                f"{len(failed_lines) + len(missing_lines)} local file check(s) failed"
            )
        else:
            checks.append(f"{len(ok_lines)} local file check(s) passed")

    # --- verdict ----------------------------------------------------------
    say()
    for line in checks:
        say(f"  PASS  {line}")
    for line in failures:
        say(f"  FAIL  {line}")
    say()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed for {manifest_path}")
        return EXIT_FAIL
    print(f"PASS: {len(checks)} check(s) passed for dataset_id {recomputed}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
