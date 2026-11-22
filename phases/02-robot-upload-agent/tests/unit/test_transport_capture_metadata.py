"""Capture metadata: `transport._metadata_from_manifest`
populates `UploadMetadata` fields 11-15 (`operator`/`site`/`permission_ref`/
`session_tags`/`task_label`) straight from the manifest -- which
`robot_storage_manager.core.load_or_create_manifest` populates from
`cfg.capture` at manifest-creation time (phase 01's own tests cover that
half of the chain; this covers the manifest -> proto half).
"""
import pytest


def _metadata_from_manifest():
    pb = pytest.importorskip(
        "upload_pb2",
        reason="generated protobuf stubs are only present after `make proto`",
    )
    from robot_upload_agent import transport as transport_mod

    return pb, transport_mod._metadata_from_manifest


def test_metadata_from_manifest_sends_empty_string_when_manifest_has_none():
    """proto3 has no distinct "unset" for a string field, so a manifest whose
    capture keys are all None (the no-`capture`-section case) must
    send "" -- the wire default the ingest server maps back to SQL NULL --
    never the literal string "None" or a missing field."""
    _, _metadata = _metadata_from_manifest()
    manifest = {
        "path": "/robot_data/demo/2024-01-01/a.mcap",
        "file_size_bytes": 3,
        "operator": None,
        "site": None,
        "permission_ref": None,
        "session_tags": None,
        "task_label": None,
    }

    md = _metadata(manifest, "rover-7")

    assert md.operator == ""
    assert md.site == ""
    assert md.permission_ref == ""
    assert md.session_tags == ""
    assert md.task_label == ""


def test_metadata_from_manifest_sends_empty_string_when_keys_are_absent():
    """A manifest dict that never had the keys at all (the exact shape a
    manifest without capture metadata has) must degrade the same way as one
    with them explicitly set to None -- `.get(...) or ""`, not a KeyError."""
    _, _metadata = _metadata_from_manifest()
    manifest = {"path": "/robot_data/demo/2024-01-01/a.mcap", "file_size_bytes": 3}

    md = _metadata(manifest, "rover-7")

    assert md.operator == ""
    assert md.site == ""
    assert md.permission_ref == ""
    assert md.session_tags == ""
    assert md.task_label == ""


def test_metadata_from_manifest_allows_a_partial_capture_set():
    """An operator who only ever configured `task_label` must not have the
    other four coerced into anything but empty."""
    _, _metadata = _metadata_from_manifest()
    manifest = {
        "path": "/robot_data/demo/2024-01-01/a.mcap",
        "file_size_bytes": 3,
        "task_label": "inspection",
    }

    md = _metadata(manifest, "rover-7")

    assert md.task_label == "inspection"
    assert md.operator == ""
    assert md.site == ""
    assert md.permission_ref == ""
    assert md.session_tags == ""
