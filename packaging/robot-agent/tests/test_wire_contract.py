"""The enrollment service declares only the RPCs that exist.

`proto/enrollment.proto` is the contract here.
"""

import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
# tests -> robot-agent -> packaging -> repo root
_REPO_ROOT = _THIS_DIR.parent.parent.parent

for _path in (_REPO_ROOT / "phases" / "02-robot-upload-agent" / "src",):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)


@pytest.fixture(scope="module")
def enrollment_pb2():
    return pytest.importorskip(
        "enrollment_pb2",
        reason="generated stubs absent; run `make proto`",
    )


def test_the_service_still_declares_only_the_rpcs_that_exist(enrollment_pb2):
    """The enrollment service implements `Enroll` only.

    The design names three RPCs for `service RobotEnrollment`; the proto
    implements `Enroll` only. If that ever changes, `credential rotate` and
    `uninstall --purge` have real work to do and this test is where the change
    surfaces.
    """
    services = list(enrollment_pb2.DESCRIPTOR.services_by_name.values())
    assert [s.name for s in services] == ["RobotEnrollment"]
    assert [m.name for m in services[0].methods] == ["Enroll"], (
        "the enrollment service grew an RPC: revisit ingotron-agent "
        "`credential rotate` and `uninstall --purge`, both of which are "
        "currently gated on its absence"
    )
