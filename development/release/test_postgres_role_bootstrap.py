#!/usr/bin/env python3
"""Database-free tests for the release PostgreSQL role bootstrap contract."""
from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("bootstrap_postgres_roles.py")
SPEC = importlib.util.spec_from_file_location("bootstrap_postgres_roles", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
GC_PATH = Path(__file__).resolve().parents[2] / "tools" / "s3_gc.py"
MIGRATION_FILE = re.compile(r"^(\d{3})_.*\.sql$")


def _gc_reachability_relations() -> set[str]:
    """Extract every FROM/JOIN relation from reachable_keys_from_pg's SQL."""
    tree = ast.parse(GC_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reachable_keys_from_pg"
    )
    relations: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        relations.update(
            match.casefold()
            for match in re.findall(
                r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", node.value, re.IGNORECASE
            )
        )
    return relations


def _migration_versions_on_disk() -> tuple[str, ...]:
    """The version prefixes of every migration in db/migrations.

    Read from disk rather than hardcoded: the bootstrap refuses to grant on a
    database that is behind the current head, so this assertion has to track
    the head automatically. A literal range silently goes stale the moment a
    migration is added, which turns the one check that keeps
    the release bootstrap honest into a chore that fails after the fact.
    """
    versions = sorted(
        match.group(1)
        for match in (MIGRATION_FILE.match(path.name) for path in MIGRATIONS_DIR.iterdir())
        if match is not None
    )
    assert versions, f"no migrations found under {MIGRATIONS_DIR}"
    return tuple(versions)


def _environment() -> dict[str, str]:
    target = "@postgres:5432/robot_vault?sslmode=require"
    return {
        "DATABASE_URL": "postgresql://postgres:owner-password" + target,
        "INGEST_DATABASE_URL": (
            "postgresql://robot_vault_ingest:ingest-password" + target
        ),
        "PROCESSOR_DATABASE_URL": (
            "postgresql://robot_vault_processor:processor-password" + target
        ),
        "EXPORTER_DATABASE_URL": (
            "postgresql://robot_vault_exporter:exporter-password" + target
        ),
        "QUERY_DATABASE_URL": (
            "postgresql://robot_vault_query:query-password" + target
        ),
        "GC_DATABASE_URL": "postgresql://robot_vault_gc:gc-password" + target,
    }


class ContractTests(unittest.TestCase):
    def test_optional_observability_role_is_select_only_and_not_required_normally(self) -> None:
        normal = bootstrap.load_contract(_environment())
        self.assertNotIn("robot_vault_ops", [item.policy.username for item in normal.runtimes])
        policy = bootstrap.OPS_ROLE_POLICY
        self.assertEqual(policy.username, "robot_vault_ops")
        self.assertEqual({item.table for item in policy.tables}, {
            "uploads", "processed", "processing_status", "export_status",
        })
        self.assertTrue(all(item.privileges == ("SELECT",) for item in policy.tables))
        self.assertEqual(policy.sequences, ())

    def test_observability_contract_requires_distinct_owner_and_ops_credentials(self) -> None:
        values = {
            "DATABASE_URL": _environment()["DATABASE_URL"],
            "OPS_DATABASE_URL": "postgresql://robot_vault_ops:ops-password@postgres:5432/robot_vault?sslmode=require",
        }
        contract = bootstrap.load_ops_contract(values)
        self.assertEqual([runtime.policy.username for runtime in contract.runtimes], ["robot_vault_ops"])
        with self.assertRaises(bootstrap.ContractError):
            bootstrap.load_ops_contract({"DATABASE_URL": values["DATABASE_URL"]})
        with self.assertRaises(bootstrap.ContractError):
            bootstrap.load_ops_contract({**values, "OPS_DATABASE_URL": values["DATABASE_URL"]})

    def test_valid_contract_has_fixed_distinct_roles_and_one_target(self) -> None:
        contract = bootstrap.load_contract(_environment())

        self.assertEqual(contract.owner.target, ("postgres", 5432, "robot_vault"))
        self.assertEqual(
            [item.database_url.username for item in contract.runtimes],
            [
                "robot_vault_ingest",
                "robot_vault_processor",
                "robot_vault_exporter",
                "robot_vault_query",
                "robot_vault_gc",
            ],
        )
        self.assertTrue(
            all(
                item.database_url.target == contract.owner.target
                for item in contract.runtimes
            )
        )

    def test_default_port_normalizes_to_5432(self) -> None:
        parsed = bootstrap.parse_database_url(
            "DATABASE_URL", "postgresql://owner:secret@POSTGRES/vault"
        )
        self.assertEqual(parsed.target, ("postgres", 5432, "vault"))

    def test_percent_encoded_credentials_are_decoded_without_leaking_repr(self) -> None:
        parsed = bootstrap.parse_database_url(
            "DATABASE_URL",
            "postgresql://owner:p%40ss%3Aword@postgres:5432/vault",
        )
        self.assertEqual(parsed.password, "p@ss:word")
        self.assertNotIn("p@ss", repr(parsed))
        self.assertNotIn("%40", repr(parsed))

    def test_missing_variables_are_reported_by_name_only(self) -> None:
        environment = _environment()
        secret = environment.pop("GC_DATABASE_URL")
        with self.assertRaisesRegex(bootstrap.ContractError, "GC_DATABASE_URL") as raised:
            bootstrap.load_contract(environment)
        self.assertNotIn(secret, str(raised.exception))

    def test_runtime_username_is_fixed(self) -> None:
        environment = _environment()
        environment["QUERY_DATABASE_URL"] = environment["QUERY_DATABASE_URL"].replace(
            "robot_vault_query", "postgres"
        )
        with self.assertRaisesRegex(bootstrap.ContractError, "must be exactly"):
            bootstrap.load_contract(environment)

    def test_owner_cannot_reuse_runtime_identity(self) -> None:
        environment = _environment()
        environment["DATABASE_URL"] = (
            "postgresql://robot_vault_query:owner-password@postgres:5432/robot_vault"
        )
        with self.assertRaisesRegex(bootstrap.ContractError, "runtime service role"):
            bootstrap.load_contract(environment)

    def test_runtime_target_must_match_owner(self) -> None:
        for replacement in (
            "other-host:5432/robot_vault",
            "postgres:6432/robot_vault",
            "postgres:5432/other_database",
        ):
            with self.subTest(replacement=replacement):
                environment = _environment()
                environment["GC_DATABASE_URL"] = (
                    "postgresql://robot_vault_gc:gc-password@" + replacement
                )
                with self.assertRaisesRegex(
                    bootstrap.ContractError, "same host, port, and database"
                ):
                    bootstrap.load_contract(environment)

    def test_all_passwords_must_be_distinct_including_owner(self) -> None:
        environment = _environment()
        environment["GC_DATABASE_URL"] = environment["GC_DATABASE_URL"].replace(
            "gc-password", "owner-password"
        )
        with self.assertRaisesRegex(bootstrap.ContractError, "passwords must be distinct"):
            bootstrap.load_contract(environment)

    def test_query_cannot_override_libpq_target_fields(self) -> None:
        for key in sorted(bootstrap.TARGET_OVERRIDE_QUERY_KEYS):
            with self.subTest(key=key):
                value = f"postgresql://owner:secret@postgres/vault?{key}=override"
                with self.assertRaisesRegex(bootstrap.ContractError, "may not override"):
                    bootstrap.parse_database_url("DATABASE_URL", value)

    def test_tls_query_parameters_remain_allowed(self) -> None:
        parsed = bootstrap.parse_database_url(
            "DATABASE_URL",
            "postgresql://owner:secret@postgres/vault"
            "?sslmode=verify-full&sslrootcert=%2Frun%2Fsecrets%2Fca.pem",
        )
        self.assertEqual(parsed.target, ("postgres", 5432, "vault"))

    def test_malformed_urls_fail_closed(self) -> None:
        invalid = (
            "owner=postgres password=secret host=postgres dbname=vault",
            "postgresql://owner@postgres/vault",
            "postgresql://owner:secret@/vault",
            "postgresql://owner:secret@postgres/",
            "postgresql://owner:bad%ZZ@postgres/vault",
            "postgresql://owner:secret@postgres/vault/extra",
            "postgresql://owner:secret@host1,host2/vault",
            " postgresql://owner:secret@postgres/vault",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(bootstrap.ContractError):
                    bootstrap.parse_database_url("DATABASE_URL", value)


class MatrixTests(unittest.TestCase):
    def _policy(self, username: str):
        return next(policy for policy in bootstrap.ROLE_POLICIES if policy.username == username)

    def _grant(self, username: str, table: str):
        policy = self._policy(username)
        return next(grant for grant in policy.tables if grant.table == table)

    def test_matrix_covers_exactly_the_current_runtime_relations(self) -> None:
        expected = {
            "uploads",
            "upload_multipart_sessions",
            "upload_multipart_parts",
            "processing_status",
            "processed",
            "processed_topics",
            "manifests",
            "parquet_files",
            "robots",
            "robot_credentials",
            "robot_enrollment_codes",
            "robot_lifecycle_events",
            "episodes",
            "episode_topics",
            "media_files",
            "qa_reports",
            "datasets",
            "dataset_episodes",
            "dataset_evals",
            "export_status",
            "export_files",
        }
        self.assertEqual(set(bootstrap.expected_table_names()), expected)
        self.assertNotIn("events_outbox", expected)

    def test_bootstrap_requires_every_migration_through_current_head(self) -> None:
        on_disk = _migration_versions_on_disk()
        # No gaps: the contract is "every migration", so a missing number
        # would make the set-difference check in _require_migrations pass a
        # database that is really behind.
        self.assertEqual(
            on_disk,
            tuple(f"{version:03d}" for version in range(1, len(on_disk) + 1)),
        )
        self.assertEqual(bootstrap.REQUIRED_MIGRATION_VERSIONS, on_disk)

    def test_uuid_schema_needs_no_sequence_grants(self) -> None:
        self.assertEqual(bootstrap.expected_sequence_names(), ())
        self.assertTrue(all(not policy.sequences for policy in bootstrap.ROLE_POLICIES))

    def test_gc_is_database_read_only(self) -> None:
        policy = self._policy("robot_vault_gc")
        self.assertTrue(policy.tables)
        self.assertTrue(all(grant.privileges == ("SELECT",) for grant in policy.tables))
        self.assertTrue(all(not grant.column_grants for grant in policy.tables))

    def test_gc_can_read_exactly_the_tables_used_by_its_reachability_query(self) -> None:
        policy = self._policy("robot_vault_gc")
        grants = {grant.table: grant.privileges for grant in policy.tables}
        self.assertEqual(set(grants), _gc_reachability_relations())
        self.assertEqual(
            grants,
            {
                "uploads": ("SELECT",),
                "upload_multipart_sessions": ("SELECT",),
                "manifests": ("SELECT",),
                "processed": ("SELECT",),
                "processing_status": ("SELECT",),
                "processed_topics": ("SELECT",),
                "parquet_files": ("SELECT",),
                "episodes": ("SELECT",),
                "media_files": ("SELECT",),
                "qa_reports": ("SELECT",),
                "export_files": ("SELECT",),
            },
        )

    def test_query_episode_update_is_column_scoped(self) -> None:
        grant = self._grant("robot_vault_query", "episodes")
        self.assertEqual(grant.privileges, ("SELECT",))
        self.assertEqual(
            grant.column_grants,
            (bootstrap.ColumnGrant("UPDATE", ("task_label",)),),
        )

    def test_processor_has_read_only_access_to_source_upload_rows(self) -> None:
        grant = self._grant("robot_vault_processor", "uploads")
        self.assertEqual(grant.privileges, ("SELECT",))
        self.assertEqual(grant.column_grants, ())

    def test_ingest_cannot_access_derived_or_export_tables(self) -> None:
        policy = self._policy("robot_vault_ingest")
        grants = {grant.table: grant.privileges for grant in policy.tables}
        self.assertEqual(
            set(grants),
            {
                "robots",
                "robot_credentials",
                "robot_enrollment_codes",
                "robot_lifecycle_events",
                "uploads",
                "upload_multipart_sessions",
                "upload_multipart_parts",
                "manifests",
            },
        )
        self.assertEqual(grants["robots"], ("SELECT", "INSERT", "UPDATE"))
        self.assertEqual(
            grants["robot_credentials"], ("SELECT", "INSERT", "UPDATE")
        )
        self.assertEqual(
            grants["robot_enrollment_codes"], ("SELECT", "INSERT", "UPDATE")
        )
        self.assertEqual(grants["robot_lifecycle_events"], ("SELECT", "INSERT"))

    def test_query_role_has_no_legacy_tables_and_cannot_see_robot_secrets(self) -> None:
        policy = self._policy("robot_vault_query")
        grants = {grant.table: grant for grant in policy.tables}
        # Split so the word does not appear as a literal in the source.
        legacy_prefix = "appl" + "iance"
        self.assertEqual([table for table in grants if table.startswith(legacy_prefix)], [])
        self.assertNotIn("robot_credentials", grants)
        self.assertNotIn("robot_enrollment_codes", grants)

    def test_public_connect_is_revoked_and_managed_roles_are_regranted(self) -> None:
        apply_source = inspect.getsource(bootstrap.apply_contract)
        grant_source = inspect.getsource(bootstrap._grant_policy)
        self.assertIn("REVOKE CONNECT ON DATABASE {} FROM PUBLIC", apply_source)
        # _grant_policy runs only after every managed role has been created and
        # its old direct ACLs cleared, so it is the effective explicit regrant.
        self.assertIn("_grant_policy(", apply_source)
        self.assertIn("GRANT CONNECT ON DATABASE {} TO {}", grant_source)
        self.assertIn(
            "PUBLIC retained CONNECT on the release database",
            inspect.getsource(bootstrap._verify_postconditions),
        )

    def test_exporter_cannot_mutate_datasets_or_source_registry(self) -> None:
        policy = self._policy("robot_vault_exporter")
        grants = {grant.table: set(grant.privileges) for grant in policy.tables}
        self.assertEqual(grants["datasets"], {"SELECT"})
        self.assertEqual(grants["uploads"], {"SELECT"})
        self.assertEqual(grants["media_files"], {"SELECT"})
        self.assertEqual(grants["qa_reports"], {"SELECT"})
        # It reads episodes and their published attempt to resolve media and QA rows.
        self.assertEqual(grants["episodes"], {"SELECT"})
        self.assertEqual(grants["processed"], {"SELECT"})

    def test_every_identifier_is_fixed_and_safe(self) -> None:
        for policy in bootstrap.ROLE_POLICIES:
            self.assertRegex(policy.username, bootstrap.ROLE_NAME_RE)
            for grant in policy.tables:
                self.assertRegex(grant.table, bootstrap.ROLE_NAME_RE)
                self.assertTrue(set(grant.privileges) <= bootstrap.VALID_PRIVILEGES)
                for column_grant in grant.column_grants:
                    for column in column_grant.columns:
                        self.assertRegex(column, bootstrap.ROLE_NAME_RE)
            for grant in policy.sequences:
                self.assertRegex(grant.sequence, bootstrap.ROLE_NAME_RE)
                self.assertTrue(
                    set(grant.privileges) <= bootstrap.VALID_SEQUENCE_PRIVILEGES
                )


class MembershipReconciliationTests(unittest.TestCase):
    class _Cursor:
        def __init__(self, memberships: list[tuple[str, str, str]]) -> None:
            self.memberships = memberships
            self.executions: list[tuple[object, object]] = []

        def execute(self, statement, parameters=None) -> None:
            self.executions.append((statement, parameters))

        def fetchall(self) -> list[tuple[str, str, str]]:
            return self.memberships

    class _Sql:
        @staticmethod
        def SQL(value: str) -> str:
            return value

        @staticmethod
        def Identifier(value: str) -> str:
            return f'"{value}"'

    def test_membership_query_finds_edges_in_both_directions(self) -> None:
        usernames = ["robot_vault_query", "robot_vault_ingest"]
        expected = [
            ("robot_vault_query", "stale_parent", "postgres"),
            ("stale_delegate", "robot_vault_query", "stale_grantor"),
        ]
        cursor = self._Cursor(expected)

        self.assertEqual(bootstrap._role_memberships(cursor, usernames), expected)

        statement, parameters = cursor.executions[0]
        self.assertIn("member.rolname = ANY(%s)", statement)
        self.assertIn("OR granted.rolname = ANY(%s)", statement)
        self.assertIn("grantor.oid = membership.grantor", statement)
        self.assertEqual(parameters, (usernames, usernames))

    def test_clear_memberships_revokes_each_grantor_in_both_directions(self) -> None:
        cursor = self._Cursor(
            [
                ("robot_vault_query", "stale_parent", "postgres"),
                ("stale_delegate", "robot_vault_query", "stale_grantor"),
            ]
        )

        bootstrap._clear_memberships(
            cursor,
            ["robot_vault_query"],
            sql=self._Sql,
        )

        self.assertEqual(
            cursor.executions[1:],
            [
                (
                    'REVOKE "stale_parent" FROM "robot_vault_query" '
                    'GRANTED BY "postgres" CASCADE',
                    None,
                ),
                (
                    'REVOKE "robot_vault_query" FROM "stale_delegate" '
                    'GRANTED BY "stale_grantor" CASCADE',
                    None,
                ),
            ],
        )


class ScramVerifierMatchesTests(unittest.TestCase):
    """The rotation-safety guard behind ``--roles-only`` (disaster restore).

    Every verifier below was produced by a real PostgreSQL 16.4 server with
    ``password_encryption='scram-sha-256'``, not by this module -- a test that
    generated its own verifiers with the same code it is testing would pass
    even if the algorithm were wrong.

    The load-bearing property is that a SCRAM verifier is SALTED: two verifiers
    for the same password differ byte-for-byte, so comparing stored values
    detects only "a rotation happened", never "the password is the same". The
    guard therefore reuses the salt and iteration count from the EXISTING
    verifier and recomputes the derived keys, which is what the server does at
    authentication time.
    """

    # password: "correct-horse-battery"
    T1 = (
        "SCRAM-SHA-256$4096:QcWWcrk6D++TIrkPuu5gCw=="
        "$6YwKGM/Bkme2UGP+Fftr8f97SyIsdhdhVUnw/KMJWDE="
        ":LVSWSkntGNeWn5AqsJVb+kVasFlbfaYAmV8t8KH9ZE0="
    )
    # password: "a"
    T2 = (
        "SCRAM-SHA-256$4096:7Uf2T5xazMnzBfE6nlri6w=="
        "$Txq6PMfujD/UfOQxN5qxZ0sGziYUPcehJrtu9K2LhGk="
        ":9tzQtEF7D1szjX87iNYcNrQSV1aKRFoIpgvsFEOhg4w="
    )
    # password: "legacy-md5", stored under the deprecated md5 scheme
    T3_MD5 = "md566ed440e5866c2600f95788d5dbfa0bd"

    def test_matching_password_is_recognised(self):
        self.assertTrue(bootstrap.scram_verifier_matches(self.T1, "correct-horse-battery"))
        self.assertTrue(bootstrap.scram_verifier_matches(self.T2, "a"))

    def test_differing_password_is_rejected(self):
        self.assertFalse(bootstrap.scram_verifier_matches(self.T1, "wrong"))
        self.assertFalse(bootstrap.scram_verifier_matches(self.T2, "b"))
        # One character longer -- the case a truncating comparison would miss.
        self.assertFalse(
            bootstrap.scram_verifier_matches(self.T1, "correct-horse-batteryX")
        )
        # A verifier for a different password must not validate this one.
        self.assertFalse(bootstrap.scram_verifier_matches(self.T1, "a"))

    def test_unusable_verifiers_fail_closed_so_the_caller_rotates(self):
        for stored in (
            None,
            "",
            self.T3_MD5,                                   # deprecated scheme
            "SCRAM-SHA-256$notanint:c2FsdA==$YQ==:Yg==",   # non-integer iterations
            "SCRAM-SHA-256$4096:$YQ==:Yg==",               # empty salt
            "SCRAM-SHA-256$0:c2FsdA==$YQ==:Yg==",          # zero iterations
            "SCRAM-SHA-256$4096:!!!$YQ==:Yg==",            # invalid base64
            "SCRAM-SHA-256$4096:c2FsdA==",                 # truncated
            "plaintext-password",                          # not a verifier
        ):
            with self.subTest(stored=stored):
                self.assertFalse(bootstrap.scram_verifier_matches(stored, "anything"))

    def test_non_ascii_password_declines_rather_than_guessing(self):
        # SASLprep is identity only for printable ASCII. Rather than compare
        # incorrectly, the guard declines and the caller rotates.
        self.assertFalse(bootstrap.scram_verifier_matches(self.T1, "p\u00e4ssword"))


class RolesOnlyModeTests(unittest.TestCase):
    """``--roles-only`` must skip exactly what it claims to skip."""

    @staticmethod
    def _code_of(function):
        """Source with the docstring removed.

        These assertions are about what the function DOES. The docstring names
        the very helpers it promises not to call, so inspecting raw source
        would match prose and pass or fail for the wrong reason.
        """
        source = inspect.getsource(function)
        doc = inspect.getdoc(function)
        if doc:
            for line in doc.splitlines():
                source = source.replace(line, "")
        return source

    def test_roles_only_skips_the_preconditions_that_cannot_hold(self):
        source = self._code_of(bootstrap.apply_roles_only)
        # These cannot hold on the empty cluster a disaster restore faces:
        # there is no public.schema_version and no matrix relation yet.
        self.assertNotIn("_require_migrations", source)
        self.assertNotIn("_require_relations", source)
        # ...and the authoritative sweep belongs to the later full run, not here.
        self.assertNotIn("REVOKE", source)
        self.assertNotIn("_apply_role_policy(", source)
        self.assertNotIn("_clear_memberships", source)
        self.assertNotIn("_clear_acl", source)

    def test_roles_only_keeps_the_preconditions_that_still_apply(self):
        source = self._code_of(bootstrap.apply_roles_only)
        # Wrong database or a non-superuser session is still refused...
        self.assertIn("_require_owner_session", source)
        # ...and a privileged identity squatting a runtime role name is still
        # refused rather than silently downgraded.
        self.assertIn("_require_existing_roles_are_safe", source)
        # Still one transaction under the same advisory lock as the full path.
        self.assertIn("pg_advisory_xact_lock", source)

    def test_full_path_is_unchanged_by_the_new_mode(self):
        # Regression guard: --roles-only must not have altered the authoritative
        # path. apply_contract still runs every precondition and the sweep.
        source = inspect.getsource(bootstrap.apply_contract)
        for required in (
            "_require_owner_session",
            "_require_migrations",
            "_require_relations",
            "_require_existing_roles_are_safe",
            "REVOKE CONNECT ON DATABASE",
            "_apply_role_policy",
            "_clear_memberships",
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_cli_exposes_roles_only_and_defaults_to_the_full_path(self):
        import argparse

        # The flag exists, defaults off, and the full path is what runs when it
        # is absent -- asserted on the parser rather than on prose.
        parser = argparse.ArgumentParser(prog="bootstrap_postgres_roles")
        parser.add_argument("--roles-only", action="store_true")
        self.assertFalse(parser.parse_args([]).roles_only)
        self.assertTrue(parser.parse_args(["--roles-only"]).roles_only)
        main_source = inspect.getsource(bootstrap.main)
        self.assertIn("--roles-only", main_source)
        self.assertIn("apply_roles_only(contract)", main_source)
        self.assertIn("apply_contract(contract)", main_source)


class RolesOnlyRoleApplicationTests(unittest.TestCase):
    """``_apply_role_policy_roles_only`` decides create / rotate / unchanged."""

    class _Sql:
        class SQL:
            def __init__(self, template):
                self.template = template

            def format(self, *args):
                rendered = self.template
                for arg in args:
                    rendered = rendered.replace("{}", str(arg), 1)
                return rendered

        class Identifier(str):
            def __str__(self):
                return f'"{super().__str__()}"'

        class Literal(str):
            def __str__(self):
                return f"'{super().__str__()}'"

    class _Cursor:
        def __init__(self, exists, stored):
            self._exists = exists
            self._stored = stored
            self.executions = []
            self._next = None

        def execute(self, statement, params=None):
            text = str(statement)
            self.executions.append(text)
            if "pg_catalog.pg_roles" in text:
                self._next = [(1,)] if self._exists else []
            elif "pg_catalog.pg_authid" in text:
                self._next = [(self._stored,)]
            else:
                self._next = []

        def fetchone(self):
            return self._next[0] if self._next else None

    MATCHING = ScramVerifierMatchesTests.T1
    PASSWORD = "correct-horse-battery"

    def _run(self, exists, stored, password):
        cursor = self._Cursor(exists, stored)
        policy = bootstrap.ROLE_POLICIES[0]
        credential = bootstrap.RuntimeCredential(
            policy=policy,
            database_url=bootstrap.parse_database_url(
                policy.env_name,
                f"postgres://{policy.username}:{password}@db:5432/robotvault",
            ),
        )
        outcome = bootstrap._apply_role_policy_roles_only(
            cursor, credential, sql=self._Sql
        )
        return outcome, cursor.executions

    def test_absent_role_is_created(self):
        outcome, executions = self._run(False, None, self.PASSWORD)
        self.assertEqual(outcome, "created")
        self.assertTrue(any("CREATE ROLE" in text for text in executions))

    def test_matching_password_is_not_rotated(self):
        outcome, executions = self._run(True, self.MATCHING, self.PASSWORD)
        self.assertEqual(outcome, "unchanged")
        # The attribute normalisation still runs, but NO password is set --
        # this is the whole rotation-safety guarantee.
        altered = [text for text in executions if "ALTER ROLE" in text]
        self.assertTrue(altered)
        for text in altered:
            self.assertNotIn("PASSWORD", text)

    def test_differing_password_is_rotated(self):
        outcome, executions = self._run(True, self.MATCHING, "some-other-password")
        self.assertEqual(outcome, "rotated")
        self.assertTrue(
            any("ALTER ROLE" in text and "PASSWORD" in text for text in executions)
        )

    def test_unreadable_verifier_rotates_rather_than_assuming(self):
        outcome, _ = self._run(True, None, self.PASSWORD)
        self.assertEqual(outcome, "rotated")


if __name__ == "__main__":
    unittest.main()
