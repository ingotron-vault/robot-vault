//! The ingest enrollment and credential tests that need a live PostgreSQL with
//! the baseline schema (`db/migrations/001_baseline.sql`) applied.
//!
//! Run them by pointing `INGEST_TEST_DATABASE_URL` at a disposable database:
//!
//! ```text
//! docker compose --profile test up -d test-postgres
//! docker compose run --rm --no-deps \
//!   -e DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault \
//!   dev python db/migrate.py
//! docker compose --profile test run --rm \
//!   -e INGEST_TEST_DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault \
//!   rust-test cargo test
//! ```
//!
//! Without the variable each test prints a loud SKIP line and returns, so the
//! default `make test-rust` gate stays runnable on a machine with no database —
//! but a skip is never silent, because a test nobody has seen fail is not
//! evidence of anything.
//!
//! **What these test that the unit tests cannot.** Every assertion below lands
//! on a *terminal state* rather than on a transition: an enrolled robot in
//! steady state whose credential authenticates and whose `first_used_at` is
//! stamped; a revoked robot mid-upload whose in-flight stream is told to abort;
//! a decommissioned robot whose credentials are all dead and whose row the API
//! can no longer move. The transitions are the easy part — it is the state the
//! system settles into that the security argument depends on.

#![cfg(test)]

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::http::{HeaderMap, StatusCode};
use axum::Json;
use deadpool_postgres::{Manager, Pool};
use serde_json::json;
use tokio::sync::Semaphore;
use tokio_postgres::NoTls;
use tonic::{Code, Request};
use uuid::Uuid;

use crate::admin_v2::{self, ProvisioningState};
use crate::enrollment::{self, EnrollmentRateLimiter, EnrollmentService};
use crate::pb;
use crate::pb::robot_enrollment_server::RobotEnrollment as _;
use crate::robot_auth::{
    self, CredentialCache, CredentialClass, RobotRegistryCache, AUTH_MODE_PER_ROBOT,
    ROBOT_STATE_DECOMMISSIONED, ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED,
};
use crate::{
    authorized_robot_filter, enforce_robot_certificate_claim, enforce_robot_resource_owner,
    recheck_stream_identity, resolve_request_identity, AppState, PresentedCredentials,
    SharedTokenPolicy,
};

/// The pin from the shared fixture vectors, so a minted code's pin
/// segment is checkable against a value three other implementations agree on.
const FIXTURE_CA_PIN: &str = "5NRHCGNQ4TR480B2FJMZQB1JYQ";
const OPERATOR_TOKEN: &str = "operator-token-long-enough-for-release-1";

struct Fixture {
    pool: Pool,
    credential_cache: CredentialCache,
    registry_cache: RobotRegistryCache,
    provisioning: ProvisioningState,
    enrollment: EnrollmentService,
}

impl Fixture {
    async fn new() -> Option<Self> {
        let Ok(url) = std::env::var("INGEST_TEST_DATABASE_URL") else {
            eprintln!(
                "SKIP: set INGEST_TEST_DATABASE_URL to a migrated disposable database to run the live-Postgres ingest tests"
            );
            return None;
        };
        let config: tokio_postgres::Config = url.parse().expect("INGEST_TEST_DATABASE_URL");
        let pool = Pool::builder(Manager::new(config, NoTls))
            .max_size(24)
            .build()
            .expect("pool");
        // Fail loudly rather than skip: an unreachable database that was
        // explicitly configured is a broken harness, not an absent one.
        let client = pool.get().await.expect("test database reachable");
        let migration_missing = client
            .query_one("SELECT 1 FROM robot_credentials LIMIT 1", &[])
            .await
            .err()
            .filter(|error| error.code().map(|code| code.code()) == Some("42P01"))
            .is_some();
        if migration_missing {
            panic!("db/migrations has not been applied to INGEST_TEST_DATABASE_URL");
        }
        drop(client);

        let credential_cache = CredentialCache::new(Duration::from_secs(30));
        let registry_cache = RobotRegistryCache::new(Duration::from_secs(30));
        let provisioning = ProvisioningState {
            pool: pool.clone(),
            credential_cache: credential_cache.clone(),
            registry_cache: registry_cache.clone(),
            ca_pin: Some(FIXTURE_CA_PIN.to_string()),
            ca_spki_sha256_hex: Some(
                "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881".to_string(),
            ),
            ingest_endpoint: "rover-vault.local:50051".to_string(),
        };
        let enrollment = EnrollmentService {
            pool: pool.clone(),
            credential_cache: credential_cache.clone(),
            registry_cache: registry_cache.clone(),
            // The limiter's own contract is unit-tested in `enrollment`; here it
            // must not be the thing that makes a 16-way concurrency test flaky.
            rate_limiter: Arc::new(EnrollmentRateLimiter::with_limits(
                Instant::now(),
                10_000.0,
                1_000.0,
                10_000.0,
                1_000.0,
                10_000,
            )),
            ingest_endpoint: "rover-vault.local:50051".to_string(),
            recommended: enrollment::RecommendedUploadSettings {
                chunk_size_bytes: 8 * 1024 * 1024,
                parallel_streams: 2,
                min_stable_age_seconds: 30.0,
            },
            self_rotation_enabled: true,
        };
        Some(Self {
            pool,
            credential_cache,
            registry_cache,
            provisioning,
            enrollment,
        })
    }

    fn app_state(&self, policy: SharedTokenPolicy) -> AppState {
        AppState {
            storage_root: std::path::PathBuf::from("/tmp/ingest-pg-tests"),
            global_sem: Arc::new(Semaphore::new(8)),
            per_robot: Arc::new(dashmap::DashMap::new()),
            per_robot_limit: 2,
            max_inflight_parts: 2,
            first_message_timeout: Duration::from_secs(10),
            stream_idle_timeout: Duration::from_secs(600),
            include_robot_labels: false,
            s3_client: None,
            s3_bucket: None,
            pg: self.pool.clone(),
            enable_file_cas: false,
            robot_registry: self.registry_cache.clone(),
            credential_cache: self.credential_cache.clone(),
            require_robot_certificate_identity: false,
            shared_token_policy: policy,
            auth_token: Some(OPERATOR_TOKEN.to_string()),
            auth_recheck_interval: Duration::from_secs(30),
        }
    }

    /// A fresh id per test, so the suite is safe to re-run against the same
    /// database and two tests never fight over one `robots` row.
    fn robot_id(&self) -> String {
        format!("t-{}", Uuid::new_v4().simple())
    }

    async fn create_robot(&self, robot_id: &str) -> (StatusCode, serde_json::Value) {
        let body = serde_json::from_value(json!({"robot_id": robot_id, "name": "Test Robot"}))
            .expect("body");
        let (status, Json(value)) =
            admin_v2::create_robot(self.provisioning.clone(), HeaderMap::new(), body).await;
        (status, value)
    }

    async fn mint_code(&self, robot_id: &str) -> (StatusCode, serde_json::Value) {
        let (status, Json(value)) = admin_v2::mint_code(
            self.provisioning.clone(),
            robot_id.to_string(),
            HeaderMap::new(),
            None,
        )
        .await;
        (status, value)
    }

    async fn code_for(&self, robot_id: &str) -> String {
        let (status, value) = self.mint_code(robot_id).await;
        assert_eq!(status, StatusCode::CREATED, "{value}");
        value["code"].as_str().expect("code").to_string()
    }

    async fn enroll(&self, code: &str) -> Result<pb::CredentialResponse, tonic::Status> {
        self.enrollment
            .enroll(Request::new(pb::EnrollRequest {
                code: code.to_string(),
                agent_version: "0.9.1".into(),
                hostname: "bench".into(),
                platform: "linux/amd64".into(),
            }))
            .await
            .map(|response| response.into_inner())
    }

    /// Create a robot, mint a code, redeem it. The steady state everything else
    /// starts from.
    async fn enrolled_robot(&self) -> (String, pb::CredentialResponse) {
        let robot_id = self.robot_id();
        assert_eq!(self.create_robot(&robot_id).await.0, StatusCode::CREATED);
        let code = self.code_for(&robot_id).await;
        let credential = self.enroll(&code).await.expect("enrollment must succeed");
        (robot_id, credential)
    }

    async fn set_state(&self, robot_id: &str, state: &str) -> (StatusCode, serde_json::Value) {
        let body = serde_json::from_value(json!({"state": state, "reason": "test"})).expect("body");
        let (status, Json(value)) = admin_v2::set_state(
            self.provisioning.clone(),
            robot_id.to_string(),
            HeaderMap::new(),
            body,
        )
        .await;
        (status, value)
    }

    async fn verify(
        &self,
        credential: &str,
    ) -> Result<robot_auth::VerifiedCredential, robot_auth::CredentialRejection> {
        robot_auth::verify_robot_credential(
            &self.pool,
            &self.credential_cache,
            &robot_auth::sha256_of(credential),
        )
        .await
        .expect("credential lookup must not error")
    }

    async fn scalar_i64(&self, sql: &str, robot_id: &str) -> i64 {
        let client = self.pool.get().await.expect("client");
        client
            .query_one(sql, &[&robot_id])
            .await
            .expect("query")
            .get(0)
    }

    async fn events(&self, robot_id: &str) -> Vec<(String, serde_json::Value)> {
        let client = self.pool.get().await.expect("client");
        client
            .query(
                "SELECT event, detail FROM robot_lifecycle_events WHERE robot_id = $1 \
                 ORDER BY occurred_at, event",
                &[&robot_id],
            )
            .await
            .expect("events")
            .into_iter()
            .map(|row| {
                (
                    row.get(0),
                    row.get::<_, Option<serde_json::Value>>(1)
                        .unwrap_or(json!({})),
                )
            })
            .collect()
    }
}

macro_rules! fixture {
    () => {
        match Fixture::new().await {
            Some(fixture) => fixture,
            None => return,
        }
    };
}

const LIVE_CREDENTIALS_SQL: &str = enrollment::LIVE_CREDENTIAL_COUNT_SQL;
const ALL_CREDENTIALS_SQL: &str = "SELECT COUNT(*) FROM robot_credentials WHERE robot_id = $1";

/// One request's presented credentials, as the interceptor would hand them over.
fn bearer(token: &str) -> PresentedCredentials {
    PresentedCredentials {
        certificate_robot_id: None,
        authorization: Some(format!("Bearer {token}")),
    }
}

fn operator_bearer() -> PresentedCredentials {
    PresentedCredentials {
        certificate_robot_id: None,
        authorization: Some(format!("Bearer {OPERATOR_TOKEN}")),
    }
}

// ===== The terminal states ==================================================

#[tokio::test]
async fn an_enrolled_robot_reaches_a_steady_state_that_authenticates_and_is_scoped() {
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;

    // The registry row settled where the exchange says it must.
    let client = fixture.pool.get().await.expect("client");
    let row = client
        .query_one(
            "SELECT state, auth_mode, enrolled_at FROM robots WHERE id = $1",
            &[&robot_id],
        )
        .await
        .expect("robot row");
    assert_eq!(row.get::<_, String>(0), ROBOT_STATE_ENROLLED);
    assert_eq!(row.get::<_, String>(1), AUTH_MODE_PER_ROBOT);
    assert!(row
        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(2)
        .is_some());

    // Exactly one credential, with the normal shape: no expiry at all.
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await, 1);
    let credential_row = client
        .query_one(
            "SELECT expires_at, superseded_at, revoked_at, issued_via, fingerprint, first_used_at \
               FROM robot_credentials WHERE robot_id = $1",
            &[&robot_id],
        )
        .await
        .expect("credential row");
    assert!(
        credential_row
            .get::<_, Option<chrono::DateTime<chrono::Utc>>>(0)
            .is_none(),
        "a credential issued by Enroll carries expires_at IS NULL"
    );
    assert!(credential_row
        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(1)
        .is_none());
    assert!(credential_row
        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(2)
        .is_none());
    assert_eq!(credential_row.get::<_, String>(3), "enrollment");
    assert_eq!(
        credential_row.get::<_, String>(4),
        credential.credential_fingerprint
    );
    assert!(
        credential_row
            .get::<_, Option<chrono::DateTime<chrono::Utc>>>(5)
            .is_none(),
        "an unused credential must be visible as unused"
    );
    drop(client);

    // The token authenticates and produces a *scoped* identity.
    let state = fixture.app_state(SharedTokenPolicy::Deny);
    let identity = resolve_request_identity(&state, bearer(&credential.credential))
        .await
        .expect("the freshly issued credential must authenticate on its first use");
    assert_eq!(identity.class, CredentialClass::Robot);
    assert_eq!(identity.robot().unwrap().robot_id(), robot_id);
    assert_eq!(
        authorized_robot_filter(identity.robot(), "").unwrap(),
        Some(robot_id.clone()),
        "an empty ListUploads filter is narrowed to this robot"
    );

    // ...and using it stamped first_used_at, which anchors the incident-review window.
    let client = fixture.pool.get().await.expect("client");
    let stamped: Option<chrono::DateTime<chrono::Utc>> = client
        .query_one(
            "SELECT first_used_at FROM robot_credentials WHERE robot_id = $1",
            &[&robot_id],
        )
        .await
        .expect("credential row")
        .get(0);
    assert!(stamped.is_some(), "authentication must stamp first_used_at");

    // The code it came from is spent, and points at the credential it minted.
    let consumed: i64 = fixture
        .scalar_i64(
            "SELECT COUNT(*) FROM robot_enrollment_codes c \
              JOIN robot_credentials r ON r.credential_id = c.consumed_credential_id \
             WHERE c.robot_id = $1 AND c.consumed_at IS NOT NULL",
            &robot_id,
        )
        .await;
    assert_eq!(consumed, 1);

    let events: BTreeSet<String> = fixture
        .events(&robot_id)
        .await
        .into_iter()
        .map(|(event, _)| event)
        .collect();
    assert!(events.contains("created"));
    assert!(events.contains("code_minted"));
    assert!(events.contains("code_redeemed"));
    assert!(events.contains("enrolled"));
}

#[tokio::test]
async fn a_revoked_robot_mid_upload_is_told_to_abort_its_stream() {
    // Revocation reaches an in-flight stream, at the decision point the
    // streaming handler calls.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let state = fixture.app_state(SharedTokenPolicy::Deny);
    let identity = resolve_request_identity(&state, bearer(&credential.credential))
        .await
        .expect("authenticated");

    // Mid-stream, before anything happens: the stream continues.
    assert_eq!(
        recheck_stream_identity(&state, &identity, &robot_id).await,
        None,
        "a live credential must not abort a healthy upload"
    );

    // The operator disables the robot — the first incident step, chosen over
    // revoking one credential precisely because it closes the whole robot.
    assert_eq!(
        fixture.set_state(&robot_id, ROBOT_STATE_DISABLED).await.0,
        StatusCode::OK
    );

    // The lifecycle write invalidated the cache synchronously, so the very next
    // chunk boundary aborts — no waiting for a TTL.
    assert_eq!(
        recheck_stream_identity(&state, &identity, &robot_id).await,
        Some("robot_not_enrolled"),
        "an in-flight stream must be aborted, not allowed to finish"
    );
    // And a new RPC from the same robot is refused outright.
    let refused = resolve_request_identity(&state, bearer(&credential.credential))
        .await
        .expect_err("a disabled robot's credential must stop authenticating");
    assert_eq!(refused.code(), Code::Unauthenticated);
    assert_eq!(refused.message(), "invalid ingest auth token");
}

#[tokio::test]
async fn disable_is_a_kill_switch_for_every_credential_including_unseen_ones() {
    // The second credential stands in for one an attacker
    // self-rotated into existence: the operator has never seen it, and
    // `state='disabled'` must still close it.
    let fixture = fixture!();
    let (robot_id, first) = fixture.enrolled_robot().await;
    let second_code = fixture.code_for(&robot_id).await;
    let second = fixture
        .enroll(&second_code)
        .await
        .expect("second credential");
    assert_eq!(fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await, 2);

    for credential in [&first.credential, &second.credential] {
        assert!(fixture.verify(credential).await.is_ok());
    }
    assert_eq!(
        fixture.set_state(&robot_id, ROBOT_STATE_DISABLED).await.0,
        StatusCode::OK
    );
    for credential in [&first.credential, &second.credential] {
        assert_eq!(
            fixture.verify(credential).await,
            Err(robot_auth::CredentialRejection::RobotNotEnrolled)
        );
    }
    // Re-enabling brings both back: `disabled` is a reversible kill switch, and
    // credentials are retained rather than destroyed.
    assert_eq!(
        fixture.set_state(&robot_id, ROBOT_STATE_ENROLLED).await.0,
        StatusCode::OK
    );
    for credential in [&first.credential, &second.credential] {
        assert!(fixture.verify(credential).await.is_ok());
    }
}

#[tokio::test]
async fn a_decommissioned_robot_is_terminal_and_all_its_credentials_are_dead() {
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    assert_eq!(
        fixture
            .set_state(&robot_id, ROBOT_STATE_DECOMMISSIONED)
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await, 0);
    assert_eq!(
        fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await,
        1,
        "revocation is a timestamp, never a row deletion — the audit anchor survives"
    );
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Revoked)
    );
    // Terminal to the API, in every direction.
    for target in [ROBOT_STATE_ENROLLED, ROBOT_STATE_DISABLED] {
        let (status, body) = fixture.set_state(&robot_id, target).await;
        assert_eq!(status, StatusCode::CONFLICT);
        assert_eq!(body["error"], "robot_decommissioned");
    }
    // And no new code can be minted for it.
    let (status, body) = fixture.mint_code(&robot_id).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"], "robot_decommissioned");
}

// ===== Enrollment code contract ==============================================

#[tokio::test]
async fn a_minted_code_carries_the_configured_ca_pin() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let (status, body) = fixture.mint_code(&robot_id).await;
    assert_eq!(status, StatusCode::CREATED);
    let code = body["code"].as_str().expect("code");
    let parsed = robot_auth::parse_enrollment_code(code).expect("a minted code must parse");
    assert_eq!(
        parsed.pin, FIXTURE_CA_PIN,
        "the minter must emit the same 26 characters /robot's b32_prefix computes"
    );
    assert_eq!(body["fingerprint"], parsed.fingerprint());
    assert_eq!(body["vault_ingest_endpoint"], "rover-vault.local:50051");
    // The plaintext is returned exactly once and only its hash is stored.
    let client = fixture.pool.get().await.expect("client");
    let stored: Vec<u8> = client
        .query_one(
            "SELECT code_sha256 FROM robot_enrollment_codes WHERE robot_id = $1",
            &[&robot_id],
        )
        .await
        .expect("code row")
        .get(0);
    assert_eq!(stored, parsed.code_sha256.to_vec());
}

#[tokio::test]
async fn a_redeemed_code_cannot_be_redeemed_again() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let code = fixture.code_for(&robot_id).await;
    fixture.enroll(&code).await.expect("first redemption");
    let replay = fixture.enroll(&code).await.expect_err("replay must fail");
    assert_eq!(replay.code(), Code::Unauthenticated);
    assert_eq!(replay.message(), enrollment::ENROLL_FAILURE_MESSAGE);
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await, 1);

    let rejected: Vec<serde_json::Value> = fixture
        .events(&robot_id)
        .await
        .into_iter()
        .filter(|(event, _)| event == "code_rejected")
        .map(|(_, detail)| detail)
        .collect();
    assert_eq!(rejected.len(), 1, "the replay must be audited");
    assert_eq!(rejected[0]["reason"], "already_consumed");
}

#[tokio::test]
async fn an_expired_code_fails_in_exactly_the_same_shape_as_an_unknown_one() {
    // Byte-compare the two responses.
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let code = fixture.code_for(&robot_id).await;
    let parsed = robot_auth::parse_enrollment_code(&code).expect("parse");
    {
        let client = fixture.pool.get().await.expect("client");
        client
            .execute(
                "UPDATE robot_enrollment_codes \
                    SET created_at = NOW() - interval '2 hours', \
                        expires_at = NOW() - interval '1 second' \
                  WHERE code_sha256 = $1",
                &[&parsed.code_sha256.as_slice()],
            )
            .await
            .expect("force expiry");
    }
    let expired = fixture.enroll(&code).await.expect_err("expired");
    let unknown_code = robot_auth::mint_enrollment_code(FIXTURE_CA_PIN).expect("mint");
    let unknown = fixture.enroll(&unknown_code).await.expect_err("unknown");
    assert_eq!(expired.code(), unknown.code());
    assert_eq!(expired.message(), unknown.message());
    assert_eq!(
        expired.details(),
        unknown.details(),
        "the failure shape must not vary with the cause"
    );
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await, 0);
}

#[tokio::test]
async fn a_revoked_code_is_refused() {
    // Through the operator-facing DELETE route.
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let (status, body) = fixture.mint_code(&robot_id).await;
    assert_eq!(status, StatusCode::CREATED);
    let code = body["code"].as_str().expect("code").to_string();
    let code_id = body["code_id"].as_str().expect("code_id").to_string();
    admin_v2::revoke_code(
        fixture.provisioning.clone(),
        robot_id.clone(),
        code_id,
        HeaderMap::new(),
    )
    .await;
    let refused = fixture.enroll(&code).await.expect_err("revoked");
    assert_eq!(refused.message(), enrollment::ENROLL_FAILURE_MESSAGE);
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn sixteen_simultaneous_redemptions_of_one_code_yield_exactly_one_credential() {
    // The property the `UPDATE … WHERE consumed_at IS NULL`
    // under the `robots` row lock exists to provide.
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let code = fixture.code_for(&robot_id).await;

    let mut tasks = Vec::new();
    for _ in 0..16 {
        let service = fixture.enrollment.clone();
        let code = code.clone();
        tasks.push(tokio::spawn(async move {
            service
                .enroll(Request::new(pb::EnrollRequest {
                    code,
                    agent_version: "0.9.1".into(),
                    hostname: "race".into(),
                    platform: "linux/amd64".into(),
                }))
                .await
                .map(|response| response.into_inner())
        }));
    }
    let mut winners = 0;
    for task in tasks {
        match task.await.expect("task must not panic") {
            Ok(_) => winners += 1,
            Err(status) => {
                assert_eq!(status.code(), Code::Unauthenticated);
                assert_eq!(
                    status.message(),
                    enrollment::ENROLL_FAILURE_MESSAGE,
                    "a losing redemption must not leak that it lost a race"
                );
            }
        }
    }
    assert_eq!(winners, 1, "exactly one caller may win");
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await, 1);
    assert_eq!(
        fixture
            .scalar_i64(
                "SELECT COUNT(*) FROM robot_enrollment_codes c \
                  JOIN robot_credentials r ON r.credential_id = c.consumed_credential_id \
                 WHERE c.robot_id = $1",
                &robot_id
            )
            .await,
        1,
        "consumed_credential_id must name the credential that was actually issued"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_codes_for_one_robot_redeem_concurrently_without_a_lost_update() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let first = fixture.code_for(&robot_id).await;
    let second = fixture.code_for(&robot_id).await;
    assert_ne!(first, second);

    let mut tasks = Vec::new();
    for code in [first, second] {
        let service = fixture.enrollment.clone();
        tasks.push(tokio::spawn(async move {
            service
                .enroll(Request::new(pb::EnrollRequest {
                    code,
                    agent_version: "0.9.1".into(),
                    hostname: "race".into(),
                    platform: "linux/amd64".into(),
                }))
                .await
        }));
    }
    for task in tasks {
        task.await.expect("task").expect("both redemptions succeed");
    }
    assert_eq!(fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await, 2);
    let enrolled_at_values: i64 = fixture
        .scalar_i64(
            "SELECT COUNT(DISTINCT enrolled_at) FROM robots WHERE id = $1",
            &robot_id,
        )
        .await;
    assert_eq!(
        enrolled_at_values, 1,
        "COALESCE(enrolled_at, NOW()) must not be overwritten by the second redemption"
    );
}

#[tokio::test]
async fn a_code_cannot_name_or_create_a_different_robot() {
    // The binding is a foreign key, not a parsing rule, and
    // `EnrollRequest` has no field that could name a robot.
    let fixture = fixture!();
    let robot_a = fixture.robot_id();
    let robot_b = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_a).await.0, StatusCode::CREATED);
    assert_eq!(fixture.create_robot(&robot_b).await.0, StatusCode::CREATED);
    let code = fixture.code_for(&robot_a).await;
    let credential = fixture.enroll(&code).await.expect("enroll");
    assert_eq!(credential.robot_id, robot_a);
    assert_eq!(fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_b).await, 0);
    let client = fixture.pool.get().await.expect("client");
    assert_eq!(
        client
            .query_one("SELECT state FROM robots WHERE id = $1", &[&robot_b])
            .await
            .expect("robot b")
            .get::<_, String>(0),
        "pending",
        "redeeming A's code must not touch B"
    );
}

#[tokio::test]
async fn a_fourth_open_code_is_refused() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    for _ in 0..enrollment::MAX_OPEN_CODES_PER_ROBOT {
        assert_eq!(fixture.mint_code(&robot_id).await.0, StatusCode::CREATED);
    }
    let (status, body) = fixture.mint_code(&robot_id).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(body["error"], "too_many_open_codes");
}

#[tokio::test]
async fn a_third_live_credential_is_refused_by_the_issuing_transaction() {
    // The cap is not an index — the liveness predicate involves
    // NOW() — so it has to hold in the transaction, under the row lock.
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let codes = [
        fixture.code_for(&robot_id).await,
        fixture.code_for(&robot_id).await,
        fixture.code_for(&robot_id).await,
    ];
    fixture.enroll(&codes[0]).await.expect("first");
    fixture.enroll(&codes[1]).await.expect("second");
    let third = fixture.enroll(&codes[2]).await.expect_err("third");
    assert_eq!(
        third.message(),
        enrollment::ENROLL_FAILURE_MESSAGE,
        "even the cap must not be distinguishable from an invalid code"
    );
    assert_eq!(fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await, 2);
    assert_eq!(
        fixture.scalar_i64(ALL_CREDENTIALS_SQL, &robot_id).await,
        2,
        "the refused attempt must leave no credential row behind"
    );
    // The third code is still unconsumed, so nothing was burned.
    let parsed = robot_auth::parse_enrollment_code(&codes[2]).expect("parse");
    let client = fixture.pool.get().await.expect("client");
    let consumed: Option<chrono::DateTime<chrono::Utc>> = client
        .query_one(
            "SELECT consumed_at FROM robot_enrollment_codes WHERE code_sha256 = $1",
            &[&parsed.code_sha256.as_slice()],
        )
        .await
        .expect("code")
        .get(0);
    assert!(
        consumed.is_none(),
        "a refused exchange must not spend the code"
    );
}

// ===== Credential verification, revocation, expiry ==========================

#[tokio::test]
async fn revocation_takes_effect_and_a_new_credential_works_immediately() {
    // Both halves: the revoked one dies, and the replacement
    // works on its FIRST attempt because negative results are never cached.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let credential_id = fixture
        .verify(&credential.credential)
        .await
        .expect("live")
        .credential_id;

    let body = serde_json::from_value(json!({"reason": "token leak"})).expect("body");
    let (status, _) = admin_v2::revoke_credential(
        fixture.provisioning.clone(),
        robot_id.clone(),
        credential_id.to_string(),
        HeaderMap::new(),
        body,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Revoked)
    );

    let replacement_code = fixture.code_for(&robot_id).await;
    let replacement = fixture.enroll(&replacement_code).await.expect("re-enroll");
    assert!(
        fixture.verify(&replacement.credential).await.is_ok(),
        "a newly issued credential must authenticate without waiting for a TTL"
    );
}

#[tokio::test]
async fn a_scheduled_revocation_sets_expires_at_and_leaves_revoked_at_null() {
    // The credential dies by the expiry predicate, not the revocation
    // one, which is why verification checks both independently.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let credential_id = fixture
        .verify(&credential.credential)
        .await
        .expect("live")
        .credential_id;
    let at = chrono::Utc::now() + chrono::Duration::hours(2);
    let body = serde_json::from_value(json!({"at": at.to_rfc3339()})).expect("body");
    let (status, _) = admin_v2::revoke_credential(
        fixture.provisioning.clone(),
        robot_id.clone(),
        credential_id.to_string(),
        HeaderMap::new(),
        body,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let client = fixture.pool.get().await.expect("client");
    let row = client
        .query_one(
            "SELECT revoked_at, expires_at FROM robot_credentials WHERE credential_id = $1",
            &[&credential_id],
        )
        .await
        .expect("credential");
    assert!(
        row.get::<_, Option<chrono::DateTime<chrono::Utc>>>(0)
            .is_none(),
        "a scheduled revocation must not set revoked_at"
    );
    assert!(row
        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(1)
        .is_some());
    fixture
        .credential_cache
        .invalidate_credential(credential_id);
    assert!(
        fixture.verify(&credential.credential).await.is_ok(),
        "before the scheduled instant the credential is still live"
    );

    // At the instant, the credential dies by the *expiry* predicate alone —
    // `revoked_at` is still NULL, which is the distinction that matters.
    client
        .execute(
            "UPDATE robot_credentials SET issued_at = NOW() - interval '3 hours', \
                    expires_at = NOW() - interval '1 second' WHERE credential_id = $1",
            &[&credential_id],
        )
        .await
        .expect("advance past the scheduled instant");
    drop(client);
    fixture
        .credential_cache
        .invalidate_credential(credential_id);
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Expired)
    );
}

#[tokio::test]
async fn an_incident_revoke_at_a_past_instant_leaves_no_live_credential() {
    // A 400 here would be the wrong failure direction: an operator who types
    // "revoke at 14:00" at 14:05
    // means revoke NOW, and erroring out leaves a live credential on a robot
    // they believe they have just cut off. The terminal state is what this
    // asserts — not that the call returned 200, but that nothing live remains.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let credential_id = fixture
        .verify(&credential.credential)
        .await
        .expect("live")
        .credential_id;
    let at = chrono::Utc::now() - chrono::Duration::minutes(5);
    let body = serde_json::from_value(json!({"at": at.to_rfc3339(), "reason": "token leak"}))
        .expect("body");
    let (status, Json(value)) = admin_v2::revoke_credential(
        fixture.provisioning.clone(),
        robot_id.clone(),
        credential_id.to_string(),
        HeaderMap::new(),
        body,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        value["applied"], "immediate",
        "the operator must be told a schedule was not set"
    );
    assert_eq!(value["revoked"], true);
    assert!(value["expires_at"].is_null());

    // The containment itself.
    assert_eq!(
        fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await,
        0,
        "a past `at` must leave NO live credential behind"
    );
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Revoked)
    );
    // It took the `revoked_at` path, so `expires_at` was never touched and
    // `robot_credentials_expiry_after_issue` was never engaged.
    let client = fixture.pool.get().await.expect("client");
    let row = client
        .query_one(
            "SELECT revoked_at, expires_at FROM robot_credentials WHERE credential_id = $1",
            &[&credential_id],
        )
        .await
        .expect("credential");
    assert!(row
        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(0)
        .is_some());
    assert!(
        row.get::<_, Option<chrono::DateTime<chrono::Utc>>>(1)
            .is_none(),
        "the immediate path must not write expires_at"
    );
    drop(client);
    // Only a malformed `at` is still a 400.
    let malformed = serde_json::from_value(json!({"at": "yesterday afternoon"})).expect("body");
    let (status, Json(value)) = admin_v2::revoke_credential(
        fixture.provisioning.clone(),
        robot_id,
        credential_id.to_string(),
        HeaderMap::new(),
        malformed,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(value["error"], "invalid_at");
}

#[tokio::test]
async fn the_schema_refuses_an_expiry_that_precedes_issue() {
    // Asserted against the baseline schema rather than assumed.
    let fixture = fixture!();
    let (robot_id, _) = fixture.enrolled_robot().await;
    let client = fixture.pool.get().await.expect("client");
    let error = client
        .execute(
            "INSERT INTO robot_credentials \
                 (robot_id, token_sha256, fingerprint, issued_via, issued_at, expires_at) \
             VALUES ($1, $2, 'aaaaaaaaaaaa', 'enrollment', NOW(), NOW() - interval '1 hour')",
            &[&robot_id, &[9u8; 32].as_slice()],
        )
        .await
        .expect_err("the constraint must refuse this row");
    assert_eq!(
        error.code().map(|code| code.code()),
        Some("23514"),
        "robot_credentials_expiry_after_issue must be the thing that refuses it"
    );
}

#[tokio::test]
async fn a_superseded_credential_dies_at_its_grace_deadline() {
    // On rows shaped exactly as a rotation leaves them, so the verification
    // predicates are proved without driving a full rotation.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let credential_id = fixture
        .verify(&credential.credential)
        .await
        .expect("live")
        .credential_id;
    let client = fixture.pool.get().await.expect("client");

    // Inside the overlap window: still live.
    client
        .execute(
            "UPDATE robot_credentials \
                SET superseded_at = NOW(), superseded_grace_until = NOW() + interval '24 hours', \
                    expires_at = NOW() + interval '24 hours' \
              WHERE credential_id = $1",
            &[&credential_id],
        )
        .await
        .expect("supersede");
    fixture
        .credential_cache
        .invalidate_credential(credential_id);
    assert!(
        fixture.verify(&credential.credential).await.is_ok(),
        "both tokens authenticate during the overlap window"
    );

    // Past it: dead, by both predicates at once (the constraint holds them equal).
    client
        .execute(
            "UPDATE robot_credentials \
                SET issued_at = NOW() - interval '48 hours', \
                    superseded_at = NOW() - interval '25 hours', \
                    superseded_grace_until = NOW() - interval '1 second', \
                    expires_at = NOW() - interval '1 second' \
              WHERE credential_id = $1",
            &[&credential_id],
        )
        .await
        .expect("expire the overlap");
    fixture
        .credential_cache
        .invalidate_credential(credential_id);
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Expired)
    );
    drop(client);
    let _ = robot_id;
}

// ===== Robot containment, with real credentials =============================

#[tokio::test]
async fn one_robots_credential_cannot_read_or_write_as_another() {
    // Driven by a credential that came out of the real
    // enrollment path rather than by a hand-built identity.
    let fixture = fixture!();
    let (robot_a, credential_a) = fixture.enrolled_robot().await;
    let (robot_b, _) = fixture.enrolled_robot().await;
    let state = fixture.app_state(SharedTokenPolicy::Deny);
    let identity = resolve_request_identity(&state, bearer(&credential_a.credential))
        .await
        .expect("A authenticates");

    // Uploading as B.
    let denied = enforce_robot_certificate_claim(identity.robot(), &robot_b)
        .expect_err("A's token must not upload as B");
    assert_eq!(denied.code(), Code::PermissionDenied);

    // Reading B's index and B's upload row.
    assert_eq!(
        authorized_robot_filter(identity.robot(), "").unwrap(),
        Some(robot_a.clone())
    );
    let denied = authorized_robot_filter(identity.robot(), &robot_b)
        .expect_err("A's token must not list B's uploads");
    assert_eq!(denied.code(), Code::PermissionDenied);
    let denied = enforce_robot_resource_owner(identity.robot(), &robot_b)
        .expect_err("A's token must not read B's upload status");
    assert_eq!(denied.code(), Code::PermissionDenied);
    assert!(enforce_robot_resource_owner(identity.robot(), &robot_a).is_ok());
}

#[tokio::test]
async fn a_certificate_and_a_bearer_that_disagree_are_refused() {
    // With mTLS *and* a bearer
    // both present, the SPIFFE id and the credential's robot id must agree
    // before either is trusted. Nothing else in the suite presents both.
    let fixture = fixture!();
    let (robot_a, credential_a) = fixture.enrolled_robot().await;
    let (robot_b, _) = fixture.enrolled_robot().await;
    let state = fixture.app_state(SharedTokenPolicy::Deny);

    let agreeing = resolve_request_identity(
        &state,
        PresentedCredentials {
            certificate_robot_id: Some(robot_a.clone()),
            authorization: Some(format!("Bearer {}", credential_a.credential)),
        },
    )
    .await
    .expect("agreeing identities authenticate");
    assert_eq!(agreeing.robot().unwrap().robot_id(), robot_a);

    let conflict = resolve_request_identity(
        &state,
        PresentedCredentials {
            certificate_robot_id: Some(robot_b.clone()),
            authorization: Some(format!("Bearer {}", credential_a.credential)),
        },
    )
    .await
    .expect_err("a certificate naming B must not be paired with A's credential");
    assert_eq!(conflict.code(), Code::PermissionDenied);
}

#[tokio::test]
async fn a_robot_disabled_while_pending_has_a_path_forward() {
    // `create → disable → enable`
    // must not strand the robot: `enable` restores the state it was disabled
    // FROM, which for a robot that never enrolled is `pending`. The terminal
    // state asserted here is not the transition — it is that the robot can then
    // complete a normal enrollment and end up uploading.
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    assert_eq!(
        fixture.set_state(&robot_id, ROBOT_STATE_DISABLED).await.0,
        StatusCode::OK
    );
    let (status, body) = fixture.set_state(&robot_id, ROBOT_STATE_ENROLLED).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["state"], "pending",
        "a robot that never enrolled is restored to pending, not to enrolled"
    );
    assert_eq!(body["requested_state"], ROBOT_STATE_ENROLLED);
    assert_eq!(body["previous_state"], ROBOT_STATE_DISABLED);

    let client = fixture.pool.get().await.expect("client");
    let row = client
        .query_one(
            "SELECT state, enrolled_at FROM robots WHERE id = $1",
            &[&robot_id],
        )
        .await
        .expect("robot");
    assert_eq!(row.get::<_, String>(0), "pending");
    assert!(
        row.get::<_, Option<chrono::DateTime<chrono::Utc>>>(1)
            .is_none(),
        "restoring to pending must not fabricate an enrolled_at"
    );
    // The action reads as one word in the audit trail whichever state it
    // restored, and records what was asked for alongside what was written.
    let enabled: Vec<serde_json::Value> = fixture
        .events(&robot_id)
        .await
        .into_iter()
        .filter(|(event, _)| event == "enabled")
        .map(|(_, detail)| detail)
        .collect();
    assert_eq!(enabled.len(), 1);
    assert_eq!(enabled[0]["requested"], ROBOT_STATE_ENROLLED);
    assert_eq!(enabled[0]["to"], "pending");
    drop(client);

    // The path forward, walked to its end: a fresh code enrolls it normally.
    let code = fixture.code_for(&robot_id).await;
    let credential = fixture
        .enroll(&code)
        .await
        .expect("the robot can now enroll");
    assert_eq!(credential.robot_id, robot_id);
    let verified = fixture.verify(&credential.credential).await.expect("live");
    assert_eq!(verified.robot_state, ROBOT_STATE_ENROLLED);

    // ...and now that it HAS enrolled, disable/enable is the ordinary
    // round trip that keeps its credentials.
    assert_eq!(
        fixture.set_state(&robot_id, ROBOT_STATE_DISABLED).await.0,
        StatusCode::OK
    );
    let (status, body) = fixture.set_state(&robot_id, ROBOT_STATE_ENROLLED).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["state"], ROBOT_STATE_ENROLLED);
    assert!(fixture.verify(&credential.credential).await.is_ok());
}

#[tokio::test]
async fn an_unknown_but_well_formed_credential_is_refused_like_garbage() {
    let fixture = fixture!();
    let state = fixture.app_state(SharedTokenPolicy::Deny);
    let unknown = robot_auth::mint_robot_credential().expect("mint");
    let refused = resolve_request_identity(&state, bearer(&unknown))
        .await
        .expect_err("an unissued credential must not authenticate");
    assert_eq!(refused.code(), Code::Unauthenticated);
    assert_eq!(refused.message(), "invalid ingest auth token");

    let garbage = resolve_request_identity(
        &state,
        PresentedCredentials {
            certificate_robot_id: None,
            authorization: Some("Bearer nonsense".to_string()),
        },
    )
    .await
    .expect_err("garbage must not authenticate");
    assert_eq!(refused.code(), garbage.code());
    assert_eq!(refused.message(), garbage.message());
}

// ===== The shared-token policy against real rows ============================

#[tokio::test]
async fn deny_refuses_the_shared_token_for_a_per_robot_robot() {
    // Under `deny` a robot must present its own token.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let state = fixture.app_state(SharedTokenPolicy::Deny);
    let operator = resolve_request_identity(&state, operator_bearer())
        .await
        .expect("the operator token still authenticates");
    assert_eq!(operator.class, CredentialClass::Operator);
    let refused = crate::authorize_upload_target(&state, &operator, &robot_id)
        .await
        .expect_err("a per_robot robot must not accept the shared token");
    assert_eq!(refused.code(), Code::Unauthenticated);

    // Its own credential still works.
    let robot = resolve_request_identity(&state, bearer(&credential.credential))
        .await
        .expect("its own credential");
    assert_eq!(
        crate::authorize_upload_target(&state, &robot, &robot_id)
            .await
            .expect("the robot's own credential is always accepted"),
        robot_id
    );
}

#[tokio::test]
async fn a_pending_robot_refuses_uploads_before_any_object_write() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    assert_eq!(fixture.create_robot(&robot_id).await.0, StatusCode::CREATED);
    let state = fixture.app_state(SharedTokenPolicy::Allow);
    let operator = crate::RequestIdentity {
        class: CredentialClass::Operator,
        robot: None,
        credential_id: None,
        credential_sha256: None,
    };
    let refused = crate::authorize_upload_target(&state, &operator, &robot_id)
        .await
        .expect_err("a pending robot may not upload");
    assert_eq!(refused.code(), Code::FailedPrecondition);
    assert!(
        refused.message().contains("robot_not_enrolled"),
        "{}",
        refused.message()
    );
}

// ===== Provisioning API =====================================================

#[tokio::test]
async fn a_duplicate_robot_id_is_a_conflict_and_mutates_nothing() {
    // Never a silent success, and never an implicit code mint.
    let fixture = fixture!();
    let (robot_id, _) = fixture.enrolled_robot().await;
    let body = serde_json::from_value(json!({
        "robot_id": robot_id, "name": "Renamed", "metadata": {"site": "bay-9"}
    }))
    .expect("body");
    let (status, Json(value)) =
        admin_v2::create_robot(fixture.provisioning.clone(), HeaderMap::new(), body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(value["error"], "robot_id_exists");
    assert_eq!(value["state"], ROBOT_STATE_ENROLLED);
    assert_eq!(value["auth_mode"], AUTH_MODE_PER_ROBOT);
    assert_eq!(value["active_credentials"], 1);

    let client = fixture.pool.get().await.expect("client");
    let row = client
        .query_one(
            "SELECT name, metadata FROM robots WHERE id = $1",
            &[&robot_id],
        )
        .await
        .expect("robot");
    assert_eq!(
        row.get::<_, String>(0),
        "Test Robot",
        "a duplicate must not rename the live robot"
    );
    assert!(row.get::<_, Option<serde_json::Value>>(1).is_none());
    // No code was minted by the conflict.
    assert_eq!(
        fixture
            .scalar_i64(
                "SELECT COUNT(*) FROM robot_enrollment_codes WHERE robot_id = $1",
                &robot_id
            )
            .await,
        1,
        "only the code the fixture minted may exist"
    );
}

#[tokio::test]
async fn the_lifecycle_listing_returns_fingerprints_and_never_a_hash() {
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let query = serde_json::from_value(json!({"robot_id": robot_id})).expect("query");
    let (status, Json(value)) = admin_v2::list_robots(fixture.provisioning.clone(), query).await;
    assert_eq!(status, StatusCode::OK);
    let robots = value["robots"].as_array().expect("robots");
    assert_eq!(robots.len(), 1);
    let entry = &robots[0];
    assert_eq!(entry["robot_id"], robot_id);
    assert_eq!(entry["state"], ROBOT_STATE_ENROLLED);
    assert_eq!(entry["auth_mode"], AUTH_MODE_PER_ROBOT);
    assert_eq!(entry["open_codes"], 0);
    let credentials = entry["credentials"].as_array().expect("credentials");
    assert_eq!(credentials.len(), 1);
    assert_eq!(
        credentials[0]["fingerprint"],
        credential.credential_fingerprint
    );
    assert_eq!(credentials[0]["issued_via"], "enrollment");
    // Nothing in the serialized response may be the secret or its hash.
    let rendered = value.to_string();
    assert!(!rendered.contains(&credential.credential));
    assert!(!rendered.contains("token_sha256"));
    assert!(!rendered.contains(&hex::encode(robot_auth::sha256_of(&credential.credential))));
}

#[tokio::test]
async fn minting_a_replacement_code_revokes_a_stranded_unused_credential() {
    // The exchange's one unremovable race, made recoverable in one operator action.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    let credential_id = fixture
        .verify(&credential.credential)
        .await
        .expect("live")
        .credential_id;
    {
        // Age the credential past the 10-minute "enrolled, never uploaded"
        // window, and clear the stamp this test's own verification just wrote.
        let client = fixture.pool.get().await.expect("client");
        client
            .execute(
                "UPDATE robot_credentials SET issued_at = NOW() - interval '30 minutes', \
                        first_used_at = NULL, last_used_at = NULL WHERE credential_id = $1",
                &[&credential_id],
            )
            .await
            .expect("age the credential");
    }
    assert_eq!(fixture.mint_code(&robot_id).await.0, StatusCode::CREATED);
    assert_eq!(
        fixture.scalar_i64(LIVE_CREDENTIALS_SQL, &robot_id).await,
        0,
        "the stranded credential must be revoked by the replacement mint"
    );
    assert_eq!(
        fixture.verify(&credential.credential).await,
        Err(robot_auth::CredentialRejection::Revoked)
    );
}

#[tokio::test]
async fn a_recently_issued_unused_credential_survives_a_second_code_mint() {
    // The other half of the rule: a robot that enrolled a minute ago and has
    // not uploaded yet must not be cut off by an operator minting a spare code.
    let fixture = fixture!();
    let (robot_id, credential) = fixture.enrolled_robot().await;
    assert_eq!(fixture.mint_code(&robot_id).await.0, StatusCode::CREATED);
    assert!(
        fixture.verify(&credential.credential).await.is_ok(),
        "a credential younger than the detection window must be left alone"
    );
}

#[tokio::test]
async fn an_untrusted_actor_header_is_stripped_before_it_reaches_the_audit_trail() {
    let fixture = fixture!();
    let robot_id = fixture.robot_id();
    let mut headers = HeaderMap::new();
    // Tab is a legal HTTP header byte and a control character, so it is the
    // one an attacker actually gets to send; the sanitizer must drop it before
    // it can shape a log or an audit row.
    headers.insert(
        admin_v2::ACTOR_HEADER,
        "alice\tbob".parse().expect("header"),
    );
    let body = serde_json::from_value(json!({"robot_id": robot_id})).expect("body");
    let (status, _) = admin_v2::create_robot(fixture.provisioning.clone(), headers, body).await;
    assert_eq!(status, StatusCode::CREATED);
    let client = fixture.pool.get().await.expect("client");
    let actor: Option<String> = client
        .query_one(
            "SELECT actor FROM robot_lifecycle_events WHERE robot_id = $1 AND event = 'created'",
            &[&robot_id],
        )
        .await
        .expect("event")
        .get(0);
    assert_eq!(actor.as_deref(), Some("alicebob"));
}
