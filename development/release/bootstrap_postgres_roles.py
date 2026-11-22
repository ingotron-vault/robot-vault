#!/usr/bin/env python3
"""Bootstrap least-privilege PostgreSQL login roles for a release deployment.

The migration owner remains the only database superuser credential.  Runtime
services receive one fixed login each and only the table/column privileges in
``ROLE_POLICIES`` below.  The bootstrap is deliberately authoritative: every
run rotates the five passwords, removes every role-membership edge touching a
runtime role plus all old direct grants, then reapplies the checked-in
privilege matrix in one transaction.

Environment contract (all six values are required PostgreSQL URLs)::

    DATABASE_URL                 # migration owner / PostgreSQL superuser
    INGEST_DATABASE_URL          # user must be robot_vault_ingest
    PROCESSOR_DATABASE_URL       # user must be robot_vault_processor
    EXPORTER_DATABASE_URL        # user must be robot_vault_exporter
    QUERY_DATABASE_URL           # user must be robot_vault_query
    GC_DATABASE_URL              # user must be robot_vault_gc

Every URL must name the same host, normalized port, and database.  Usernames
and passwords must be present, all passwords must be distinct, and libpq query
parameters may not override the URL's connection target.  Percent-encode URL
reserved characters in credentials (for example ``@`` as ``%40``).

Run this only after ``db/migrate.py``.  It fails closed unless every migration
in ``REQUIRED_MIGRATION_VERSIONS`` (today the ``001`` baseline) and every
relation named by the grant matrix are present.  No default
runtime grants are installed: after a future migration, rerun this bootstrap;
a new table remains inaccessible until its required privileges are reviewed
and added here.

``--roles-only`` is the one exception to that ordering, and it exists for
exactly one caller: a disaster restore onto an empty cluster.  There the
cluster has been recreated from an empty volume, so there is no
``public.schema_version``, no relation the grant matrix names, and none of the
five runtime roles -- and the restore cannot proceed until the roles exist,
because ``pg_restore`` has to connect as one.  In that mode the bootstrap
creates or repairs the five login roles from the supplied DSNs and does
nothing else: no migration precondition, no relation precondition, no
authoritative grant sweep.  Privileges are re-derived by a later full run,
once the relations the matrix names have been restored.

``--roles-only`` is also ROTATION-SAFE, which the full path deliberately is
not.  A restore reinstates the backup's credentials; rotating a password that
already matches would invalidate the very DSNs the caller just restored.  So a
role whose stored SCRAM verifier already validates the supplied password is
left untouched.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit


DEFAULT_POSTGRES_PORT = 5432
BOOTSTRAP_ADVISORY_LOCK_KEY = int.from_bytes(b"RV_ROLES", "big", signed=True)
REQUIRED_MIGRATION_VERSIONS = ("001",)

# A query parameter can override the authority/path encoded in a libpq URL.
# Reject those rather than comparing a target which libpq may later replace.
TARGET_OVERRIDE_QUERY_KEYS = frozenset(
    {
        "dbname",
        "host",
        "hostaddr",
        "passfile",
        "password",
        "port",
        "service",
        "servicefile",
        "user",
    }
)
VALID_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)
VALID_SEQUENCE_PRIVILEGES = frozenset({"USAGE", "SELECT", "UPDATE"})
ALL_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class BootstrapError(RuntimeError):
    """A safe-to-print release role bootstrap failure."""


class ContractError(BootstrapError):
    """The release environment does not satisfy the credential contract."""


class SchemaError(BootstrapError):
    """The target database is not at the schema expected by this utility."""


@dataclass(frozen=True)
class ColumnGrant:
    privilege: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.privilege not in VALID_PRIVILEGES:
            raise ValueError(f"unsupported column privilege {self.privilege!r}")
        if not self.columns:
            raise ValueError("a column grant must name at least one column")


@dataclass(frozen=True)
class TableGrant:
    table: str
    privileges: tuple[str, ...] = ()
    column_grants: tuple[ColumnGrant, ...] = ()

    def __post_init__(self) -> None:
        unknown = set(self.privileges) - VALID_PRIVILEGES
        if unknown:
            raise ValueError(f"unsupported table privileges: {sorted(unknown)}")
        if not ROLE_NAME_RE.fullmatch(self.table):
            raise ValueError(f"unsafe table identifier {self.table!r}")


@dataclass(frozen=True)
class SequenceGrant:
    sequence: str
    privileges: tuple[str, ...]

    def __post_init__(self) -> None:
        unknown = set(self.privileges) - VALID_SEQUENCE_PRIVILEGES
        if unknown:
            raise ValueError(f"unsupported sequence privileges: {sorted(unknown)}")
        if not self.privileges:
            raise ValueError("a sequence grant must name at least one privilege")
        if not ROLE_NAME_RE.fullmatch(self.sequence):
            raise ValueError(f"unsafe sequence identifier {self.sequence!r}")


@dataclass(frozen=True)
class RolePolicy:
    env_name: str
    username: str
    tables: tuple[TableGrant, ...]
    sequences: tuple[SequenceGrant, ...] = ()

    def __post_init__(self) -> None:
        if not ROLE_NAME_RE.fullmatch(self.username):
            raise ValueError(f"unsafe fixed role identifier {self.username!r}")
        table_names = [grant.table for grant in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError(f"duplicate table in {self.username} policy")
        sequence_names = [grant.sequence for grant in self.sequences]
        if len(sequence_names) != len(set(sequence_names)):
            raise ValueError(f"duplicate sequence in {self.username} policy")


def _table(
    name: str,
    *privileges: str,
    columns: tuple[ColumnGrant, ...] = (),
) -> TableGrant:
    return TableGrant(name, tuple(privileges), columns)


# Keep this matrix next to the bootstrap, not in Compose or a shell fragment.
# It is the reviewable source of truth for runtime PostgreSQL authority.
ROLE_POLICIES: tuple[RolePolicy, ...] = (
    RolePolicy(
        "INGEST_DATABASE_URL",
        "robot_vault_ingest",
        (
            _table("robots", "SELECT", "INSERT", "UPDATE"),
            _table("robot_credentials", "SELECT", "INSERT", "UPDATE"),
            _table("robot_enrollment_codes", "SELECT", "INSERT", "UPDATE"),
            _table("robot_lifecycle_events", "SELECT", "INSERT"),
            _table("uploads", "SELECT", "INSERT", "UPDATE"),
            _table("upload_multipart_sessions", "SELECT", "INSERT", "UPDATE", "DELETE"),
            _table("upload_multipart_parts", "SELECT", "INSERT", "UPDATE", "DELETE"),
            _table("manifests", "SELECT", "INSERT"),
        ),
    ),
    RolePolicy(
        "PROCESSOR_DATABASE_URL",
        "robot_vault_processor",
        (
            _table("uploads", "SELECT"),
            _table("robots", "SELECT"),
            _table("processing_status", "SELECT", "INSERT", "UPDATE"),
            _table("processed", "SELECT", "INSERT", "UPDATE"),
            _table(
                "processed_topics", "SELECT", "INSERT", "UPDATE", "DELETE"
            ),
            _table("parquet_files", "SELECT", "INSERT", "UPDATE", "DELETE"),
            _table("episodes", "SELECT", "INSERT", "UPDATE", "DELETE"),
            _table("episode_topics", "SELECT", "INSERT"),
            _table("media_files", "SELECT", "INSERT", "UPDATE"),
            _table("qa_reports", "SELECT", "INSERT", "DELETE"),
        ),
    ),
    RolePolicy(
        "EXPORTER_DATABASE_URL",
        "robot_vault_exporter",
        (
            _table("export_status", "SELECT", "UPDATE"),
            _table("export_files", "SELECT", "INSERT", "UPDATE", "DELETE"),
            _table("datasets", "SELECT"),
            # Read-only for the reserved exporter identity: datasets and episodes.
            _table("episodes", "SELECT"),
            _table("processed", "SELECT"),
            _table("media_files", "SELECT"),
            _table("qa_reports", "SELECT"),
            _table("uploads", "SELECT"),
        ),
    ),
    RolePolicy(
        "QUERY_DATABASE_URL",
        "robot_vault_query",
        (
            _table("robots", "SELECT"),
            _table("uploads", "SELECT"),
            _table("processing_status", "SELECT"),
            _table("processed", "SELECT"),
            _table("processed_topics", "SELECT"),
            _table("parquet_files", "SELECT"),
            _table(
                "episodes",
                "SELECT",
                columns=(
                    ColumnGrant("UPDATE", ("task_label",)),
                ),
            ),
            _table("episode_topics", "SELECT"),
            _table("media_files", "SELECT"),
            _table("qa_reports", "SELECT"),
            _table("datasets", "SELECT", "INSERT"),
            _table("dataset_episodes", "SELECT", "INSERT"),
            _table("dataset_evals", "SELECT", "INSERT"),
            _table("export_status", "SELECT", "INSERT"),
            _table("export_files", "SELECT"),
        ),
    ),
    RolePolicy(
        "GC_DATABASE_URL",
        "robot_vault_gc",
        (
            _table("uploads", "SELECT"),
            _table("upload_multipart_sessions", "SELECT"),
            _table("manifests", "SELECT"),
            _table("processed", "SELECT"),
            _table("processing_status", "SELECT"),
            _table("processed_topics", "SELECT"),
            _table("parquet_files", "SELECT"),
            _table("episodes", "SELECT"),
            _table("media_files", "SELECT"),
            _table("qa_reports", "SELECT"),
            _table("export_files", "SELECT"),
        ),
    ),
)

# Optional observability profile. It is bootstrapped separately so existing
# deployments need no new credential when the profile is disabled. Its four
# SELECT grants are the entire SQL surface of tools.operational_metrics.
OPS_ROLE_POLICY = RolePolicy(
    "OPS_DATABASE_URL",
    "robot_vault_ops",
    (
        _table("uploads", "SELECT"),
        _table("processed", "SELECT"),
        _table("processing_status", "SELECT"),
        _table("export_status", "SELECT"),
    ),
)


@dataclass(frozen=True)
class DatabaseUrl:
    env_name: str
    dsn: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    host: str
    port: int
    database: str

    @property
    def target(self) -> tuple[str, int, str]:
        return (self.host.casefold(), self.port, self.database)


@dataclass(frozen=True)
class RuntimeCredential:
    policy: RolePolicy
    database_url: DatabaseUrl


@dataclass(frozen=True)
class ReleaseContract:
    owner: DatabaseUrl
    runtimes: tuple[RuntimeCredential, ...]


def _decode_url_component(raw: str, *, env_name: str, component: str) -> str:
    if INVALID_PERCENT_ESCAPE_RE.search(raw):
        raise ContractError(f"{env_name} has an invalid percent escape in {component}")
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            f"{env_name} has non-UTF-8 percent-encoding in {component}"
        ) from exc
    if not decoded:
        raise ContractError(f"{env_name} must include a non-empty {component}")
    if "\x00" in decoded:
        raise ContractError(f"{env_name} {component} may not contain NUL")
    return decoded


def parse_database_url(env_name: str, value: str) -> DatabaseUrl:
    """Parse the strict URL subset used by release Compose.

    This intentionally does not accept keyword/value libpq DSNs: target
    identity must be visible and comparable before a network connection is
    made, and runtime credentials must stay in the URL's userinfo.
    """
    if not value:
        raise ContractError(f"{env_name} is required")
    if value != value.strip():
        raise ContractError(f"{env_name} may not have surrounding whitespace")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ContractError(f"{env_name} is not a valid PostgreSQL URL") from exc
    if parts.scheme.casefold() not in {"postgres", "postgresql"}:
        raise ContractError(f"{env_name} must use postgres:// or postgresql://")
    if parts.fragment:
        raise ContractError(f"{env_name} may not contain a URL fragment")
    if parts.username is None or parts.password is None:
        raise ContractError(f"{env_name} must include username and password")

    username = _decode_url_component(
        parts.username, env_name=env_name, component="username"
    )
    password = _decode_url_component(
        parts.password, env_name=env_name, component="password"
    )
    try:
        host = parts.hostname
        port = parts.port or DEFAULT_POSTGRES_PORT
    except ValueError as exc:
        raise ContractError(f"{env_name} has an invalid host or port") from exc
    if not host:
        raise ContractError(f"{env_name} must include a host")
    if "," in host:
        raise ContractError(f"{env_name} may name only one PostgreSQL host")
    if not parts.path.startswith("/") or parts.path == "/":
        raise ContractError(f"{env_name} must include a database name")
    raw_database = parts.path[1:]
    if "/" in raw_database:
        raise ContractError(f"{env_name} database path must contain one segment")
    database = _decode_url_component(
        raw_database, env_name=env_name, component="database name"
    )

    try:
        query_items = (
            parse_qsl(
                parts.query,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
            )
            if parts.query
            else []
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError(f"{env_name} has an invalid URL query") from exc
    overrides = sorted(
        {key.casefold() for key, _value in query_items}
        & TARGET_OVERRIDE_QUERY_KEYS
    )
    if overrides:
        raise ContractError(
            f"{env_name} query may not override connection target field(s): "
            + ", ".join(overrides)
        )

    return DatabaseUrl(
        env_name=env_name,
        dsn=value,
        username=username,
        password=password,
        host=host.casefold(),
        port=port,
        database=database,
    )


def load_contract(environ: Mapping[str, str]) -> ReleaseContract:
    required_names = ("DATABASE_URL",) + tuple(
        policy.env_name for policy in ROLE_POLICIES
    )
    missing = [name for name in required_names if not environ.get(name)]
    if missing:
        raise ContractError(
            "missing required PostgreSQL credential variable(s): "
            + ", ".join(missing)
        )

    owner = parse_database_url("DATABASE_URL", environ["DATABASE_URL"])
    fixed_usernames = {policy.username for policy in ROLE_POLICIES}
    if owner.username in fixed_usernames:
        raise ContractError("DATABASE_URL may not use a runtime service role")

    runtimes: list[RuntimeCredential] = []
    for policy in ROLE_POLICIES:
        database_url = parse_database_url(policy.env_name, environ[policy.env_name])
        if database_url.username != policy.username:
            raise ContractError(
                f"{policy.env_name} username must be exactly {policy.username!r}"
            )
        if database_url.target != owner.target:
            raise ContractError(
                f"{policy.env_name} must target the same host, port, and database "
                "as DATABASE_URL"
            )
        runtimes.append(RuntimeCredential(policy, database_url))

    usernames = [owner.username] + [item.database_url.username for item in runtimes]
    if len(usernames) != len(set(usernames)):
        raise ContractError("PostgreSQL owner and runtime usernames must be distinct")
    passwords = [owner.password] + [item.database_url.password for item in runtimes]
    if len(passwords) != len(set(passwords)):
        raise ContractError("PostgreSQL owner and runtime passwords must be distinct")

    return ReleaseContract(owner=owner, runtimes=tuple(runtimes))


def load_ops_contract(environ: Mapping[str, str]) -> ReleaseContract:
    """Validate the two credentials needed by the optional metrics profile."""
    for name in ("DATABASE_URL", OPS_ROLE_POLICY.env_name):
        if not environ.get(name):
            raise ContractError(f"{name} is required for the observability role")
    owner = parse_database_url("DATABASE_URL", environ["DATABASE_URL"])
    ops = parse_database_url(OPS_ROLE_POLICY.env_name, environ[OPS_ROLE_POLICY.env_name])
    if owner.username == OPS_ROLE_POLICY.username or ops.username != OPS_ROLE_POLICY.username:
        raise ContractError("OPS_DATABASE_URL must use the fixed robot_vault_ops role")
    if owner.target != ops.target:
        raise ContractError("OPS_DATABASE_URL must target the owner database")
    if owner.password == ops.password:
        raise ContractError("observability and owner passwords must be distinct")
    return ReleaseContract(owner=owner, runtimes=(RuntimeCredential(OPS_ROLE_POLICY, ops),))


def expected_table_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                grant.table
                for policy in ROLE_POLICIES
                for grant in policy.tables
            }
        )
    )


def expected_sequence_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                grant.sequence
                for policy in ROLE_POLICIES
                for grant in policy.sequences
            }
        )
    )


def _require_owner_session(cur, owner: DatabaseUrl) -> None:
    cur.execute(
        """
        SELECT current_user, session_user, current_database(), role.rolsuper
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """
    )
    row = cur.fetchone()
    if row is None:
        raise BootstrapError("connected PostgreSQL owner role is not visible")
    current_user, session_user, database, is_superuser = row
    if current_user != owner.username or session_user != owner.username:
        raise BootstrapError(
            "DATABASE_URL session/current user does not match its URL username"
        )
    if database != owner.database:
        raise BootstrapError("DATABASE_URL connected to an unexpected database")
    if not is_superuser:
        raise BootstrapError("DATABASE_URL role must be a PostgreSQL superuser")


def _require_migrations(cur) -> None:
    cur.execute("SELECT to_regclass('public.schema_version')")
    row = cur.fetchone()
    if row is None or row[0] is None:
        raise SchemaError("public.schema_version is missing; run db/migrate.py first")
    cur.execute("SELECT version FROM public.schema_version")
    applied = {str(row[0]) for row in cur.fetchall()}
    missing = sorted(set(REQUIRED_MIGRATION_VERSIONS) - applied)
    if missing:
        raise SchemaError(
            "required migration(s) are not applied: " + ", ".join(missing)
        )


def _require_relations(cur) -> None:
    table_names = expected_table_names()
    sequence_names = expected_sequence_names()
    all_names = tuple(sorted(set(table_names) | set(sequence_names)))
    cur.execute(
        """
        SELECT relation.relname, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relname = ANY(%s)
        """,
        (list(all_names),),
    )
    found = {str(name): str(kind) for name, kind in cur.fetchall()}
    missing_tables = [name for name in table_names if name not in found]
    wrong_tables = [
        name for name in table_names if name in found and found[name] not in {"r", "p"}
    ]
    missing_sequences = [name for name in sequence_names if name not in found]
    wrong_sequences = [
        name for name in sequence_names if name in found and found[name] != "S"
    ]
    problems: list[str] = []
    if missing_tables:
        problems.append("missing table(s): " + ", ".join(missing_tables))
    if wrong_tables:
        problems.append("non-table relation(s): " + ", ".join(wrong_tables))
    if missing_sequences:
        problems.append("missing sequence(s): " + ", ".join(missing_sequences))
    if wrong_sequences:
        problems.append("non-sequence relation(s): " + ", ".join(wrong_sequences))
    if problems:
        raise SchemaError("; ".join(problems))


def _existing_role_rows(cur, usernames: Sequence[str]) -> dict[str, tuple[bool, ...]]:
    cur.execute(
        """
        SELECT rolname, rolsuper, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = ANY(%s)
        """,
        (list(usernames),),
    )
    return {
        str(row[0]): tuple(bool(flag) for flag in row[1:])
        for row in cur.fetchall()
    }


def _require_existing_roles_are_safe(cur, usernames: Sequence[str]) -> None:
    rows = _existing_role_rows(cur, usernames)
    flag_names = ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS")
    for username, flags in rows.items():
        enabled = [name for name, value in zip(flag_names, flags) if value]
        if enabled:
            raise BootstrapError(
                f"existing runtime role {username!r} has prohibited attribute(s): "
                + ", ".join(enabled)
                + "; refusing to silently downgrade a privileged identity"
            )

    # Ownership cannot be removed by REVOKE and would silently defeat the ACL
    # matrix.  Reject it before rotating a password or changing any grant.
    cur.execute(
        """
        SELECT owner.rolname, namespace.nspname || '.' || relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
        WHERE owner.rolname = ANY(%s)
          AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        UNION ALL
        SELECT owner.rolname, 'schema ' || namespace.nspname
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner
        WHERE owner.rolname = ANY(%s)
        UNION ALL
        SELECT owner.rolname, 'database ' || database.datname
        FROM pg_catalog.pg_database AS database
        JOIN pg_catalog.pg_roles AS owner ON owner.oid = database.datdba
        WHERE owner.rolname = ANY(%s)
        ORDER BY 1, 2
        """,
        (list(usernames), list(usernames), list(usernames)),
    )
    owned = [(str(role), str(object_name)) for role, object_name in cur.fetchall()]
    if owned:
        role, object_name = owned[0]
        raise BootstrapError(
            f"existing runtime role {role!r} owns {object_name}; ownership must be "
            "transferred before least-privilege bootstrap"
        )


def _role_memberships(cur, usernames: Sequence[str]) -> list[tuple[str, str, str]]:
    """Return ``(member, granted_role, grantor)`` grants touching a runtime.

    Both directions matter.  A runtime role that is a member of another role
    can inherit or assume that role's privileges; a different principal that
    is a member of a runtime role can assume the service identity.  The
    authoritative release contract permits neither relationship.
    """
    runtime_roles = list(usernames)
    cur.execute(
        """
        SELECT member.rolname, granted.rolname, grantor.rolname
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS grantor ON grantor.oid = membership.grantor
        WHERE member.rolname = ANY(%s)
           OR granted.rolname = ANY(%s)
        ORDER BY member.rolname, granted.rolname, grantor.rolname
        """,
        (runtime_roles, runtime_roles),
    )
    return [
        (str(member), str(granted), str(grantor))
        for member, granted, grantor in cur.fetchall()
    ]


def scram_verifier_matches(stored: str | None, password: str) -> bool:
    """Does ``stored`` (a pg_authid.rolpassword value) validate ``password``?

    PostgreSQL stores a SCRAM-SHA-256 verifier as::

        SCRAM-SHA-256$<iterations>:<b64 salt>$<b64 StoredKey>:<b64 ServerKey>

    The salt is random per rotation, so the verifier cannot be recomputed from
    the password alone -- but it CAN be recomputed using the salt and iteration
    count already inside the stored value, which is exactly what the server
    does when authenticating.  That makes "is this the same password?" an
    offline, deterministic question with no connection and no rotation.

    Fails CLOSED: anything unparseable, unknown, non-ASCII, or MD5 returns
    False, so the caller rotates.  Rotating unnecessarily is a recoverable
    annoyance; skipping a rotation that was needed would leave a role whose
    password does not match its DSN, which is the failure this whole mode
    exists to avoid.
    """
    if not stored or not stored.startswith("SCRAM-SHA-256$"):
        # None (no password), an MD5 verifier, or an unrecognised scheme.
        return False
    try:
        # SASLprep is the identity mapping for printable ASCII.  Anything else
        # would need a real SASLprep implementation to compare correctly, so
        # decline rather than guess.
        password.encode("ascii")
    except UnicodeEncodeError:
        return False

    import base64
    import hashlib
    import hmac

    try:
        _, remainder = stored.split("$", 1)
        iteration_text, salt_and_keys = remainder.split(":", 1)
        salt_b64, keys = salt_and_keys.split("$", 1)
        stored_key_b64, server_key_b64 = keys.split(":", 1)
        iterations = int(iteration_text)
        salt = base64.b64decode(salt_b64, validate=True)
        stored_key = base64.b64decode(stored_key_b64, validate=True)
        server_key = base64.b64decode(server_key_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if iterations <= 0 or not salt:
        return False

    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    expected_stored_key = hashlib.sha256(client_key).digest()
    expected_server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return hmac.compare_digest(expected_stored_key, stored_key) and hmac.compare_digest(
        expected_server_key, server_key
    )


def _stored_verifier(cur, username: str) -> str | None:
    """The role's current rolpassword, or None when the role does not exist."""
    cur.execute(
        "SELECT rolpassword FROM pg_catalog.pg_authid WHERE rolname = %s",
        (username,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _apply_role_policy_roles_only(cur, credential: RuntimeCredential, *, sql) -> str:
    """Create or repair one login role without touching any privilege.

    Returns one of ``"created"``, ``"rotated"`` or ``"unchanged"`` so the
    caller can report -- and so the tests can assert on the decision rather
    than inferring it.
    """
    policy = credential.policy
    password = credential.database_url.password
    role_identifier = sql.Identifier(policy.username)
    attributes = (
        "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1"
    )

    cur.execute(
        "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (policy.username,)
    )
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL("CREATE ROLE {} WITH " + attributes + " PASSWORD {} VALID UNTIL {}").format(
                role_identifier,
                sql.Literal(password),
                sql.Literal("infinity"),
            )
        )
        return "created"

    if scram_verifier_matches(_stored_verifier(cur, policy.username), password):
        # The stored verifier already validates this password.  Normalise the
        # role's attributes -- which is cheap and idempotent -- but do NOT
        # touch the password.  The disaster restore depends on this
        # rotation-safety guarantee.
        cur.execute(
            sql.SQL("ALTER ROLE {} WITH " + attributes + " VALID UNTIL {}").format(
                role_identifier, sql.Literal("infinity")
            )
        )
        return "unchanged"

    cur.execute(
        sql.SQL("ALTER ROLE {} WITH " + attributes + " PASSWORD {} VALID UNTIL {}").format(
            role_identifier,
            sql.Literal(password),
            sql.Literal("infinity"),
        )
    )
    return "rotated"


def _apply_role_policy(cur, credential: RuntimeCredential, *, sql) -> None:
    policy = credential.policy
    password = credential.database_url.password
    role_identifier = sql.Identifier(policy.username)

    cur.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (policy.username,))
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 "
                "PASSWORD {} VALID UNTIL {}"
            ).format(
                role_identifier,
                sql.Literal(password),
                sql.Literal("infinity"),
            )
        )
    else:
        cur.execute(
            sql.SQL(
                "ALTER ROLE {} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 "
                "PASSWORD {} VALID UNTIL {}"
            ).format(
                role_identifier,
                sql.Literal(password),
                sql.Literal("infinity"),
            )
        )
    # Remove per-role/session defaults left by an earlier manual role. In
    # particular, a startup `role` or `search_path` setting must not make the
    # effective runtime identity differ from this bootstrap's ACL model.
    cur.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(role_identifier))
    cur.execute(
        sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(
            role_identifier,
            sql.Identifier(credential.database_url.database),
        )
    )


def _clear_memberships(cur, usernames: Sequence[str], *, sql) -> None:
    for member, granted, grantor in _role_memberships(cur, usernames):
        cur.execute(
            sql.SQL("REVOKE {} FROM {} GRANTED BY {} CASCADE").format(
                sql.Identifier(granted),
                sql.Identifier(member),
                sql.Identifier(grantor),
            )
        )


def _clear_acl(cur, username: str, database: str, *, sql) -> None:
    role = sql.Identifier(username)
    cur.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
            sql.Identifier(database), role
        )
    )
    cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(role))
    cur.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}"
        ).format(role)
    )
    cur.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
        ).format(role)
    )
    cur.execute(
        sql.SQL(
            "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {}"
        ).format(role)
    )
    _clear_column_acl(cur, role, sql=sql)


def _clear_column_acl(cur, grantee, *, sql) -> None:
    """Remove column grants, which table-level ``REVOKE ALL`` does not cover."""
    cur.execute(
        """
        SELECT namespace.nspname, relation.relname,
               array_agg(attribute.attname ORDER BY attribute.attnum)
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        GROUP BY namespace.nspname, relation.relname
        ORDER BY namespace.nspname, relation.relname
        """
    )
    for schema_name, relation_name, columns in cur.fetchall():
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            cur.execute(
                sql.SQL("REVOKE {} ({}) ON TABLE {} FROM {}").format(
                    sql.SQL(privilege),
                    sql.SQL(", ").join(sql.Identifier(str(column)) for column in columns),
                    sql.Identifier(str(schema_name), str(relation_name)),
                    grantee,
                )
            )


def _clear_default_acl(cur, owner: str, runtime: str, *, sql) -> None:
    for object_kind in ("TABLES", "SEQUENCES"):
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE ALL PRIVILEGES ON {} FROM {}"
            ).format(
                sql.Identifier(owner),
                sql.SQL(object_kind),
                sql.Identifier(runtime),
            )
        )


def _grant_policy(cur, policy: RolePolicy, database: str, *, sql) -> None:
    role = sql.Identifier(policy.username)
    cur.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(database), role
        )
    )
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    for grant in policy.tables:
        relation = sql.Identifier("public", grant.table)
        if grant.privileges:
            cur.execute(
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(item) for item in grant.privileges),
                    relation,
                    role,
                )
            )
        for column_grant in grant.column_grants:
            cur.execute(
                sql.SQL("GRANT {} ({}) ON TABLE {} TO {}").format(
                    sql.SQL(column_grant.privilege),
                    sql.SQL(", ").join(
                        sql.Identifier(column) for column in column_grant.columns
                    ),
                    relation,
                    role,
                )
            )
    for grant in policy.sequences:
        cur.execute(
            sql.SQL("GRANT {} ON SEQUENCE {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(item) for item in grant.privileges),
                sql.Identifier("public", grant.sequence),
                role,
            )
        )


def _verify_postconditions(cur, contract: ReleaseContract) -> None:
    usernames = [item.policy.username for item in contract.runtimes]
    rows = _existing_role_rows(cur, usernames)
    if set(rows) != set(usernames):
        raise BootstrapError("one or more runtime roles were not created")
    if any(any(flags) for flags in rows.values()):
        raise BootstrapError("a runtime role retained a prohibited role attribute")
    cur.execute(
        """
        SELECT rolname, rolcanlogin, rolinherit, rolconnlimit
        FROM pg_catalog.pg_roles
        WHERE rolname = ANY(%s)
        ORDER BY rolname
        """,
        (usernames,),
    )
    login_rows = cur.fetchall()
    if len(login_rows) != len(usernames) or any(
        not can_login or does_inherit or int(connection_limit) != -1
        for _role, can_login, does_inherit, connection_limit in login_rows
    ):
        raise BootstrapError("a runtime role retained unsafe login/inheritance settings")
    if _role_memberships(cur, usernames):
        raise BootstrapError("a role membership involving a runtime role remained")

    cur.execute(
        "SELECT has_database_privilege('public', %s, 'CONNECT')",
        (contract.owner.database,),
    )
    if cur.fetchone()[0]:
        raise BootstrapError("PUBLIC retained CONNECT on the release database")

    cur.execute(
        """
        SELECT relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
        ORDER BY relation.relname
        """
    )
    all_tables = tuple(str(row[0]) for row in cur.fetchall())
    for credential in contract.runtimes:
        policy = credential.policy
        expected = {grant.table: grant for grant in policy.tables}
        cur.execute(
            """
            SELECT has_database_privilege(%s, %s, 'CONNECT'),
                   has_database_privilege(%s, %s, 'TEMPORARY'),
                   has_schema_privilege(%s, 'public', 'USAGE'),
                   has_schema_privilege(%s, 'public', 'CREATE')
            """,
            (
                policy.username,
                contract.owner.database,
                policy.username,
                contract.owner.database,
                policy.username,
                policy.username,
            ),
        )
        connect, temporary, usage, create = cur.fetchone()
        if not connect or temporary or not usage or create:
            raise BootstrapError(
                f"effective database/schema ACL check failed for {policy.username}"
            )

        for table in all_tables:
            table_grant = expected.get(table)
            expected_privileges = set(table_grant.privileges if table_grant else ())
            relation = f"public.{table}"
            for privilege in ALL_TABLE_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (policy.username, relation, privilege),
                )
                actual = bool(cur.fetchone()[0])
                if actual != (privilege in expected_privileges):
                    raise BootstrapError(
                        f"effective {privilege} ACL check failed for "
                        f"{policy.username} on {relation}"
                    )

            column_expectations = {
                (column_grant.privilege, column)
                for column_grant in (table_grant.column_grants if table_grant else ())
                for column in column_grant.columns
            }
            cur.execute(
                """
                SELECT attribute.attname
                FROM pg_catalog.pg_attribute AS attribute
                WHERE attribute.attrelid = %s::regclass
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                ORDER BY attribute.attnum
                """,
                (relation,),
            )
            columns = [str(row[0]) for row in cur.fetchall()]
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                # A table-level grant already covers every column. Only check
                # column ACL exactness when that broader grant is absent.
                if privilege in expected_privileges:
                    continue
                for column in columns:
                    cur.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        (policy.username, relation, column, privilege),
                    )
                    actual = bool(cur.fetchone()[0])
                    expected_column = (privilege, column) in column_expectations
                    if actual != expected_column:
                        raise BootstrapError(
                            f"effective {privilege} column ACL check failed for "
                            f"{policy.username} on {relation}.{column}"
                        )


def apply_roles_only(
    contract: ReleaseContract,
    *,
    connect: Callable[..., object] | None = None,
) -> dict[str, str]:
    """Create or repair the five runtime login roles and nothing else.

    For a disaster restore.  Deliberately does NOT run ``_require_migrations``
    or ``_require_relations`` -- the restore calls this against a cluster that
    has neither -- and deliberately does NOT run the authoritative grant
    sweep, which belongs to the later full run once the relations exist.

    Returns ``{username: "created" | "rotated" | "unchanged"}``.
    """
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - release image carries it
        raise BootstrapError("psycopg is required to bootstrap PostgreSQL roles") from exc

    connector = connect or psycopg.connect
    outcomes: dict[str, str] = {}
    connection_context = connector(
        contract.owner.dsn,
        autocommit=True,
        connect_timeout=10,
    )
    with connection_context as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                )
                cur.execute("SELECT set_config('lock_timeout', '10s', true)")
                cur.execute("SELECT set_config('statement_timeout', '60s', true)")
                cur.execute(
                    "SELECT set_config('password_encryption', 'scram-sha-256', true)"
                )

                # The two preconditions that remain meaningful on an empty
                # cluster both still run.  _require_owner_session keeps this
                # from being pointed at the wrong database or run by a
                # non-superuser; _require_existing_roles_are_safe keeps it from
                # silently downgrading a privileged identity that happens to
                # share a runtime role name.  What is skipped is skipped
                # because it CANNOT hold here, not because it is inconvenient.
                _require_owner_session(cur, contract.owner)
                usernames = [item.policy.username for item in contract.runtimes]
                _require_existing_roles_are_safe(cur, usernames)

                for credential in contract.runtimes:
                    outcomes[credential.policy.username] = _apply_role_policy_roles_only(
                        cur, credential, sql=sql
                    )
    return outcomes


def apply_contract(
    contract: ReleaseContract,
    *,
    connect: Callable[..., object] | None = None,
) -> None:
    """Apply ``contract`` atomically using its owner URL.

    ``connect`` is an injection seam for tests.  Production imports psycopg
    lazily so URL/matrix unit tests have no database or driver dependency.
    """
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - release image carries it
        raise BootstrapError("psycopg is required to bootstrap PostgreSQL roles") from exc

    connector = connect or psycopg.connect
    try:
        connection_context = connector(
            contract.owner.dsn,
            autocommit=True,
            connect_timeout=10,
        )
        with connection_context as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                    )
                    cur.execute("SELECT set_config('lock_timeout', '10s', true)")
                    cur.execute("SELECT set_config('statement_timeout', '60s', true)")
                    cur.execute(
                        "SELECT set_config('password_encryption', 'scram-sha-256', true)"
                    )

                    # Every precondition runs before the first mutation. Any
                    # error after that still rolls the whole transaction back.
                    _require_owner_session(cur, contract.owner)
                    _require_migrations(cur)
                    _require_relations(cur)
                    usernames = [item.policy.username for item in contract.runtimes]
                    _require_existing_roles_are_safe(cur, usernames)

                    # Remove privileges inherited through PUBLIC before
                    # validating the exact role ACLs below. The release DB is
                    # dedicated to Robot Vault; migration owner/superuser
                    # authority is unaffected by these revocations.
                    cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                    cur.execute(
                        sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                            sql.Identifier(contract.owner.database)
                        )
                    )
                    cur.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                            sql.Identifier(contract.owner.database)
                        )
                    )
                    cur.execute(
                        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC"
                    )
                    cur.execute(
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC"
                    )
                    _clear_column_acl(cur, sql.SQL("PUBLIC"), sql=sql)

                    for credential in contract.runtimes:
                        _apply_role_policy(cur, credential, sql=sql)
                    _clear_memberships(cur, usernames, sql=sql)

                    for credential in contract.runtimes:
                        _clear_acl(
                            cur,
                            credential.policy.username,
                            contract.owner.database,
                            sql=sql,
                        )
                        _clear_default_acl(
                            cur,
                            contract.owner.username,
                            credential.policy.username,
                            sql=sql,
                        )
                        _grant_policy(
                            cur,
                            credential.policy,
                            contract.owner.database,
                            sql=sql,
                        )

                    # Do not let future owner-created relations accidentally
                    # become PUBLIC-readable. Runtime roles receive no default
                    # grant; the checked-in matrix must be rerun and reviewed.
                    for object_kind in ("TABLES", "SEQUENCES"):
                        cur.execute(
                            sql.SQL(
                                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                                "REVOKE ALL PRIVILEGES ON {} FROM PUBLIC"
                            ).format(
                                sql.Identifier(contract.owner.username),
                                sql.SQL(object_kind),
                            )
                        )

                    _verify_postconditions(cur, contract)
    except BootstrapError:
        raise
    except Exception as exc:
        # Do not echo libpq DSNs or a composed ALTER ROLE statement: either
        # can contain the password currently being rotated.
        raise BootstrapError(
            "PostgreSQL runtime-role bootstrap failed "
            f"({type(exc).__name__}); no credential value was logged"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="bootstrap_postgres_roles",
        description=(
            "Apply the checked-in PostgreSQL privilege matrix, or (with "
            "--roles-only) create the runtime login roles alone."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--roles-only",
        action="store_true",
        help=(
            "Create or repair the five runtime login roles and nothing else. "
            "Skips the migration and relation preconditions and the grant "
            "sweep, and does not rotate a password that already matches. For "
            "a disaster restore against an empty cluster -- a later full run "
            "re-derives every privilege."
        ),
    )
    mode.add_argument(
        "--ops-only",
        action="store_true",
        help="Apply only the optional SELECT-only observability role after migrations.",
    )
    args = parser.parse_args(argv)

    try:
        contract = load_ops_contract(os.environ) if args.ops_only else load_contract(os.environ)
        if args.roles_only:
            outcomes = apply_roles_only(contract)
        else:
            apply_contract(contract)
    except BootstrapError as exc:
        print(f"postgres role bootstrap: ERROR: {exc}", file=sys.stderr)
        return 1

    if args.roles_only:
        summary = ", ".join(f"{name}={state}" for name, state in sorted(outcomes.items()))
        print(f"postgres role bootstrap: roles ready ({summary})")
        print(
            "postgres role bootstrap: NO privileges were applied; run without "
            "--roles-only once the relations exist"
        )
        return 0

    role_names = ", ".join(item.policy.username for item in contract.runtimes)
    print(f"postgres role bootstrap: ready ({role_names})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
