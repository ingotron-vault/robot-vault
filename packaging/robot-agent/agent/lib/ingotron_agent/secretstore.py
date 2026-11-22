"""Credential files on the robot.

    | /etc/ingotron/credentials/           | 0700 | ingotron:ingotron |
    | /etc/ingotron/credentials/robot.token| 0600 | ingotron:ingotron | ignr1...,
                                                                       no
                                                                       trailing
                                                                       newline

Writes are `open(O_CREAT|O_EXCL, 0600)` -> write -> `fsync` -> `fsync(dir)`;
a replacement writes `robot.token.new` and `rename(2)`s over, so the file is
never briefly empty or absent.  Both halves matter: the O_EXCL create is what
makes "enroll over an existing enrollment" a refusal rather than a silent
clobber, and the fsync pair is what makes a robot that loses power mid-write
come back with either the old token or the new one and never with zero bytes.
"""

import grp
import os
import pwd
from pathlib import Path
from typing import Optional, Tuple

TOKEN_MODE = 0o600
DIR_MODE = 0o700


class SecretWriteError(RuntimeError):
    pass


def resolve_service_ids(user: str, group: str) -> Tuple[Optional[int], Optional[int]]:
    """(uid, gid) for the service account, or (None, None) if it does not exist.

    Missing is not an error here: `enroll` may legitimately run in a test
    sandbox with no ingotron account, and the mode bits are the property that
    matters in that case.  robot.sh creates the account before it ever calls
    enroll on a real host.
    """
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        uid = None
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        gid = None
    return uid, gid


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _chown_best_effort(path: Path, uid: Optional[int], gid: Optional[int]) -> bool:
    if uid is None or gid is None:
        return False
    try:
        os.chown(str(path), uid, gid)
        return True
    except PermissionError:
        # Not root.  The mode is still 0600, so the secret is not exposed; the
        # caller reports this so a gate can tell "wrong owner" from "no owner
        # change attempted".
        return False


def ensure_credential_dir(directory: Path, uid: Optional[int], gid: Optional[int]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(str(directory), DIR_MODE)
    _chown_best_effort(directory, uid, gid)


def write_secret(
    path: Path,
    secret: str,
    *,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    replace: bool = False,
) -> None:
    """Write `secret` to `path` with the credential durability and mode contract.

    `replace=False` (the default, and what `enroll` uses) refuses to overwrite:
    O_EXCL means a second enrollment cannot silently replace a live credential.
    `replace=True` (what a rotation uses) writes `<path>.new` and renames over,
    so a reader never observes a missing or truncated file.
    """
    if secret != secret.strip() or not secret:
        raise SecretWriteError("refusing to store a blank or untrimmed credential")
    if "\n" in secret or "\r" in secret:
        raise SecretWriteError("refusing to store a credential containing a newline")

    ensure_credential_dir(path.parent, uid, gid)

    target = Path(str(path) + ".new") if replace else path
    if replace and target.exists():
        target.unlink()

    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, TOKEN_MODE)
    try:
        # No trailing newline: the file IS the token.
        os.write(fd, secret.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)

    # umask can clear bits that the open() mode requested, so set them again.
    os.chmod(str(target), TOKEN_MODE)
    _chown_best_effort(target, uid, gid)

    if replace:
        os.replace(str(target), str(path))
    _fsync_dir(path.parent)
