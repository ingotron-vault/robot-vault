"""The Ingotron robot-side agent runtime.

This package is what `agent-linux-amd64.tar.gz` carries.  It drives the
existing `rsm` (phases/01-robot-storage) and `rua` (phases/02-robot-upload-agent)
CLIs rather than reimplementing them, and it owns exactly the pieces those CLIs
do not have: enrollment, per-robot credential storage, the single-instance
guard, and the systemd surface.
"""

__all__ = ["AGENT_VERSION"]

# Overwritten at build time by build-agent-tarball.sh, which writes the release
# version into VERSION next to this package.  The fallback is only ever seen in
# a source checkout.
AGENT_VERSION = "0.0.0-dev"

try:  # pragma: no cover - trivial file read
    import pathlib as _pathlib
    import sys as _sys

    if getattr(_sys, "frozen", False):
        # onedir layout: <release>/bin/ingotron-agent + bin/_internal/*
        _version_file = _pathlib.Path(_sys.executable).resolve().parent.parent / "VERSION"
    else:
        _version_file = _pathlib.Path(__file__).resolve().parent.parent.parent / "VERSION"
    if _version_file.is_file():
        _text = _version_file.read_text(encoding="utf-8").strip()
        if _text:
            AGENT_VERSION = _text
except OSError:
    pass
