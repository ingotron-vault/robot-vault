use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::{
    extract::Query,
    routing::{get, post},
    Json, Router,
};
use chrono::{Datelike, Utc};
use prost_types::Timestamp;
// SQLite removed
use aws_sdk_s3 as s3;
use aws_sdk_s3::primitives::ByteStream;
use aws_types::region::Region as AwsRegion;
use dashmap::DashMap;
use deadpool_postgres::{Manager, Object, Pool};
use metrics::{counter, describe_counter, describe_gauge, describe_histogram, gauge, histogram};
use metrics_exporter_prometheus::PrometheusBuilder;
use percent_encoding::percent_decode_str;
use sha2::{Digest, Sha256};
use tokio::sync::Semaphore;
use tokio_postgres::NoTls;
use tokio_stream::StreamExt;
use tonic::{
    service::interceptor::InterceptedService,
    transport::{Certificate, Identity, Server, ServerTlsConfig},
    Request, Response, Status,
};
use tonic_health::server::health_reporter;
use tonic_reflection::server::Builder as ReflectionBuilder;
use tracing::{error, info, warn};
use uuid::Uuid;
use x509_parser::{extensions::GeneralName, parse_x509_certificate};

pub mod pb {
    tonic::include_proto!("robotics.upload.v1");
}

mod admin_v2;
mod crockford;
mod enrollment;
#[cfg(test)]
mod pg_tests;
mod resumable;
mod robot_auth;
mod security_profile;

use pb::upload_service_server::{UploadService, UploadServiceServer};
use pb::{UploadRequest, UploadResponse, UploadSmallRequest};
use robot_auth::{
    AuthenticatedRobot, CredentialCache, CredentialClass, RequestIdentity, RobotRegistration,
    RobotRegistryCache,
};
use security_profile::{
    profile_configuration_error, resolve_ingest_security_profile, resolve_shared_token_policy,
    SharedTokenPolicy, DEV_DEFAULT_TOKEN,
};

#[derive(Clone)]
struct AppState {
    storage_root: PathBuf,
    global_sem: Arc<Semaphore>,
    per_robot: Arc<DashMap<String, Arc<Semaphore>>>,
    per_robot_limit: usize,
    /// How many S3 `upload_part` calls one streaming upload may have in
    /// flight at once. See the "S3 part pipelining" block below.
    max_inflight_parts: usize,
    /// Maximum time a streaming caller may occupy a transport-admission slot
    /// without sending its metadata message. No in-handler permit is held yet.
    first_message_timeout: Duration,
    /// Maximum gap between messages after an upload has acquired capacity.
    stream_idle_timeout: Duration,
    include_robot_labels: bool,
    // Optional integrations
    s3_client: Option<s3::Client>,
    s3_bucket: Option<String>,
    // Required Postgres
    pg: Pool,
    enable_file_cas: bool,
    /// TTL cache of each robot's lifecycle row.
    ///
    /// It caches the lifecycle state, not mere existence, and only for the TTL,
    /// so a `disabled` robot stops uploading once its entry expires. Misses
    /// always re-query, so a robot registered a moment ago works on its very
    /// next attempt.
    robot_registry: RobotRegistryCache,
    /// TTL cache of verified per-robot credentials, keyed by `SHA-256(token)`.
    /// Positive-only for the same reason: a newly issued credential must work
    /// on its first attempt.
    credential_cache: CredentialCache,
    /// Release mTLS certificates identify exactly one robot through a URI SAN.
    /// Only the `mtls` profile requires this. Under `token` the identity
    /// comes from the per-robot bearer credential.
    require_robot_certificate_identity: bool,
    /// The profile-derived shared-token data-plane policy. Admin authorization
    /// remains the distinct operator-token boundary.
    shared_token_policy: SharedTokenPolicy,
    /// A non-empty token enables shared-token operator authentication. Keeping
    /// the startup-read value in state lets per-request auth first attempt the
    /// shared path before a per-robot credential lookup.
    auth_token: Option<String>,
    /// How often a streaming `Upload` re-verifies its credential. Equal
    /// to the credential cache TTL, and clamped so the deployment knob can only
    /// tighten the 30 s guarantee, never loosen it.
    auth_recheck_interval: Duration,
}

#[derive(Clone)]
struct IngestSvc {
    state: AppState,
}

const UPLOAD_STREAM_RPC_PATH: &str = "/robotics.upload.v1.UploadService/Upload";
const UPLOAD_SMALL_RPC_PATH: &str = "/robotics.upload.v1.UploadService/UploadSmall";

fn is_payload_upload_rpc(path: &str) -> bool {
    matches!(path, UPLOAD_STREAM_RPC_PATH | UPLOAD_SMALL_RPC_PATH)
}

/// Process-wide admission for the two payload-bearing upload RPCs. Tonic
/// decodes protobuf bodies inside the generated service future, so this wrapper
/// takes a permit before calling that service and holds it until the RPC
/// finishes. Clones made for separate HTTP/2 connections share the semaphore.
/// Status and compatibility RPCs bypass it because their bodies are small.
///
/// Capacity is checked in `call`, not queued in `poll_ready`: an overloaded
/// request receives `RESOURCE_EXHAUSTED` without entering tonic's decoder.
#[derive(Clone)]
struct UploadTransportAdmission<S> {
    inner: S,
    permits: Arc<Semaphore>,
}

impl<S> UploadTransportAdmission<S> {
    fn new(inner: S, capacity: usize) -> Self {
        Self {
            inner,
            permits: Arc::new(Semaphore::new(capacity)),
        }
    }
}

impl<S> tonic::server::NamedService for UploadTransportAdmission<S>
where
    S: tonic::server::NamedService,
{
    const NAME: &'static str = S::NAME;
}

impl<S, B> tonic::codegen::Service<tonic::codegen::http::Request<B>> for UploadTransportAdmission<S>
where
    S: tonic::codegen::Service<
        tonic::codegen::http::Request<B>,
        Response = tonic::codegen::http::Response<tonic::body::BoxBody>,
        Error = std::convert::Infallible,
    >,
    S::Future: Send + 'static,
{
    type Response = tonic::codegen::http::Response<tonic::body::BoxBody>;
    type Error = std::convert::Infallible;
    type Future = tonic::codegen::BoxFuture<Self::Response, Self::Error>;

    fn poll_ready(
        &mut self,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(context)
    }

    fn call(&mut self, request: tonic::codegen::http::Request<B>) -> Self::Future {
        if !is_payload_upload_rpc(request.uri().path()) {
            return Box::pin(self.inner.call(request));
        }
        let permit = match self.permits.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                counter!("ingest_transport_admission_rejected_total").increment(1);
                return Box::pin(async {
                    Ok(Status::resource_exhausted("ingest request capacity exceeded").to_http())
                });
            }
        };
        let future = self.inner.call(request);
        Box::pin(async move {
            let response = future.await;
            drop(permit);
            response
        })
    }
}

// ========== Completed-row write gates success ==========
//
// The just-written S3 object is only reachable by the pipeline through its
// `completed` row in Postgres. If that row cannot be persisted, the RPC must NOT
// answer `completed` — otherwise the client marks the file uploaded while the
// object sits in S3 invisible to the processor forever (silent data loss). This
// pure helper encodes that decision so it can be unit-tested without a database:
// a confirmed write yields the completed response; a failed write yields
// `Status::internal` so the client retries.
#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn finalize_completion<E: std::fmt::Display>(
    resp: UploadResponse,
    completed_row_write: Result<(), E>,
) -> Result<UploadResponse, Status> {
    match completed_row_write {
        Ok(()) => Ok(resp),
        Err(e) => Err(Status::internal(format!(
            "completed_row_write_failed: {}",
            e
        ))),
    }
}

/// S3 caps a multipart upload at 10000 parts. Chunk `n` maps to part `n + 1`.
const MAX_PARTS: u64 = 10000;

const MEBIBYTE: usize = 1024 * 1024;
/// Streaming clients are told to use chunks no larger than 8 MiB during
/// enrollment. Enforce the same ceiling on every caller before a chunk can be
/// retained by the S3 upload pipeline.
const STREAM_CHUNK_CEILING_BYTES: usize = 8 * MEBIBYTE;
/// `UploadSmall` still supports the existing 48 MiB client threshold, so the
/// service-wide tonic decoder cannot use the smaller streaming ceiling.
const GRPC_MESSAGE_CEILING_BYTES: usize = 50 * MEBIBYTE;

// ========== S3 part pipelining ==========
//
// A streaming upload validates each chunk on the receive path and hands only
// the S3 `upload_part` call to a bounded `JoinSet` (see resumable.rs). When
// the bound is reached, the loop stops reading the stream until a part lands.
// That backpressure bounds retained payload at
// `GLOBAL_CONCURRENCY x (MAX_INFLIGHT_PARTS x 8 MiB + 50 MiB)`. The 50 MiB
// term is the current tonic-decoded message, which exists before the handler
// can enforce the smaller streaming chunk ceiling.

/// Default number of S3 part uploads allowed in flight per stream. Four parts
/// is enough to keep the S3 round-trip covered by the arrival of the next chunk
/// while keeping the memory bound small on a host with little free RAM; most of
/// the gain is captured by the time the third or fourth part is in flight.
const DEFAULT_MAX_INFLIGHT_PARTS: usize = 4;

/// Hard ceiling on the configured value. Every in-flight part pins one chunk of
/// memory per concurrent stream, so an operator typo (`MAX_INFLIGHT_PARTS=5000`)
/// must not be able to turn a fast client into an OOM.
const MAX_INFLIGHT_PARTS_CEILING: usize = 32;

const DEFAULT_PER_ROBOT_CONCURRENCY: usize = 2;
const DEFAULT_GLOBAL_CONCURRENCY: usize = 8;
/// Each streaming upload can retain several chunks while S3 parts are in
/// flight. Keep startup configuration finite so a typo cannot allocate an
/// effectively unbounded semaphore and database pool.
const MAX_PER_ROBOT_CONCURRENCY: usize = 16;
const MAX_GLOBAL_CONCURRENCY: usize = 64;

/// Release Compose gives ingest a 2 GiB container by default. Limit upload
/// payload to half of that amount, leaving the other half for protobuf copies,
/// TLS, the AWS SDK, Postgres, task stacks, and allocator overhead.
const REFERENCE_INGEST_CONTAINER_BYTES: usize = 2 * 1024 * MEBIBYTE;
const MAX_INGEST_UPLOAD_PAYLOAD_BYTES: usize = REFERENCE_INGEST_CONTAINER_BYTES / 2;

const DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS: f64 = 10.0;
const DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS: f64 = 600.0;
const MAX_STREAM_TIMEOUT_SECONDS: f64 = 3600.0;
/// Parse one upload-concurrency setting. Unlike `MAX_INFLIGHT_PARTS`, these
/// values size long-lived semaphores and the Postgres pool, so invalid values
/// are configuration errors rather than candidates for silent clamping.
fn resolve_concurrency_limit(
    name: &str,
    raw: Option<&str>,
    default: usize,
    maximum: usize,
) -> Result<usize, String> {
    let Some(raw) = raw else {
        return Ok(default);
    };
    let value = raw
        .trim()
        .parse::<usize>()
        .map_err(|_| format!("{name} must be an integer in 1..={maximum}; got {raw:?}"))?;
    if !(1..=maximum).contains(&value) {
        return Err(format!("{name} must be in 1..={maximum}; got {value}"));
    }
    Ok(value)
}

/// The `host:port` a robot is told to upload to. The port is the one robots
/// reach from outside (`INGEST_PUBLIC_PORT`, the published Compose port), not
/// the container's listen port; unset or empty falls back to the latter.
fn public_ingest_endpoint(
    host: Option<&str>,
    public_port: Option<&str>,
    listen_port: u16,
) -> Result<String, String> {
    let port = match public_port.map(str::trim).filter(|port| !port.is_empty()) {
        None => listen_port,
        Some(raw) => raw
            .parse::<u16>()
            .ok()
            .filter(|port| *port != 0)
            .ok_or_else(|| {
                format!("INGEST_PUBLIC_PORT must be a port in 1..=65535; got {raw:?}")
            })?,
    };
    let host = host.map(str::trim).filter(|host| !host.is_empty());
    Ok(match host {
        // An explicit `host:port` already names the external port.
        Some(host) if host.contains(':') => host.to_string(),
        Some(host) => format!("{host}:{port}"),
        None => format!("localhost:{port}"),
    })
}

fn read_concurrency_limit(name: &str, default: usize, maximum: usize) -> anyhow::Result<usize> {
    let raw = match std::env::var(name) {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(std::env::VarError::NotUnicode(_)) => {
            anyhow::bail!("{name} must be valid UTF-8")
        }
    };
    resolve_concurrency_limit(name, raw.as_deref(), default, maximum).map_err(anyhow::Error::msg)
}

fn resolve_timeout_seconds(
    name: &str,
    raw: Option<&str>,
    default_seconds: f64,
    maximum_seconds: f64,
) -> Result<Duration, String> {
    let seconds = match raw {
        None => default_seconds,
        Some(raw) => raw.trim().parse::<f64>().map_err(|_| {
            format!("{name} must be a number in (0, {maximum_seconds}]; got {raw:?}")
        })?,
    };
    if !seconds.is_finite() || seconds <= 0.0 || seconds > maximum_seconds {
        return Err(format!(
            "{name} must be finite and in (0, {maximum_seconds}]; got {seconds}"
        ));
    }
    Ok(Duration::from_secs_f64(seconds))
}

fn read_timeout_seconds(
    name: &str,
    default_seconds: f64,
    maximum_seconds: f64,
) -> anyhow::Result<Duration> {
    let raw = match std::env::var(name) {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(std::env::VarError::NotUnicode(_)) => {
            anyhow::bail!("{name} must be valid UTF-8")
        }
    };
    resolve_timeout_seconds(name, raw.as_deref(), default_seconds, maximum_seconds)
        .map_err(anyhow::Error::msg)
}

/// Resolve `MAX_INFLIGHT_PARTS`. Unset or unparseable gives the default; 0 is
/// raised to 1 (zero would mean "never upload a part"), and an over-large value
/// is clamped rather than rejected so a bad config degrades instead of stopping
/// the server from booting.
fn resolve_max_inflight_parts(raw: Option<&str>) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .map(|n| n.clamp(1, MAX_INFLIGHT_PARTS_CEILING))
        .unwrap_or(DEFAULT_MAX_INFLIGHT_PARTS)
}

/// Validate the combined upload settings against the payload budget. Tonic
/// must decode a message before the streaming handler can enforce its 8 MiB
/// data ceiling, so each active call can transiently hold its bounded S3 parts
/// plus one message at the larger service-wide decode limit.
fn validate_upload_payload_budget(
    global_concurrency: usize,
    max_inflight_parts: usize,
) -> Result<usize, String> {
    let per_stream_bytes = max_inflight_parts
        .checked_mul(STREAM_CHUNK_CEILING_BYTES)
        .and_then(|parts| parts.checked_add(GRPC_MESSAGE_CEILING_BYTES))
        .ok_or_else(|| "MAX_INFLIGHT_PARTS overflows the payload calculation".to_string())?;
    let payload_bytes = global_concurrency
        .checked_mul(per_stream_bytes)
        .ok_or_else(|| "upload settings overflow the retained-payload calculation".to_string())?;

    if payload_bytes > MAX_INGEST_UPLOAD_PAYLOAD_BYTES {
        return Err(format!(
            "GLOBAL_CONCURRENCY={global_concurrency} and MAX_INFLIGHT_PARTS={max_inflight_parts} can hold {} MiB of upload payload including current decoded messages; the limit is {} MiB. Lower either setting",
            payload_bytes / MEBIBYTE,
            MAX_INGEST_UPLOAD_PAYLOAD_BYTES / MEBIBYTE,
        ));
    }
    Ok(payload_bytes)
}

// ========== Robot registry enforcement ==========
//
// The schema has a `robots` table and a FK from `uploads.robot_id` to it.
// This server therefore has to decide what to do with an upload whose robot is
// not registered. The two candidates were reject and quarantine; this is a
// REJECT, decided before a single byte reaches S3, for three reasons:
//
//   1. Quarantine cannot actually be implemented behind that FK. "Quarantined"
//      still means writing an `uploads` row — that row is the only handle the
//      rest of the system has on a stored object — and the FK refuses to insert
//      one for an unregistered robot. Making quarantine work would mean either
//      dropping the constraint (leaving fleet identity advisory again, which is
//      the thing this check exists to fix) or auto-inserting the unknown robot, which
//      *is* trust-on-first-use and turns the registry back into a log of
//      whatever happened to arrive.
//   2. Rejecting early is the only option that does not create garbage. Without
//      this check the upload would run to completion, put the object in S3, and
//      only then fail on the `uploads` insert — which the completed-row gate turns into
//      `Status::internal` *after* the bytes are stored, leaving an orphaned
//      object no row references and GC has to sweep. The check runs before the
//      S3 client is even touched.
//   3. Nothing is lost by rejecting. The upload agent marks the file `failed`
//      and leaves it on the robot (see core.py `run_once`), and retention never
//      deletes a file that has not been uploaded. The data waits on the robot
//      until an operator registers it — which is the correct outcome for a
//      machine nobody has provisioned, and is recoverable, unlike silently
//      admitting a typo'd id into the lake as a new "robot".
//
// `FAILED_PRECONDITION` (not `INVALID_ARGUMENT`) is the honest gRPC code: per
// the spec it means the system state must be fixed before a retry can succeed,
// which is exactly right — the request is well-formed, the fleet registry just
// does not have this robot yet.

/// Why a robot id was refused before the upload started.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RobotIdError {
    /// The client sent no robot id at all.
    Missing,
    /// The literal `"unknown"` placeholder. Robot identity is a required
    /// config value, so this name means "the client does not know who it is"
    /// and must never be accepted as a fleet member's name.
    UnknownSentinel,
    /// Longer than `uploads.robot_id` / `robots.id` (VARCHAR(50)) can hold, so
    /// Postgres would reject it anyway — with a much worse error.
    TooLong,
    /// Contains a path separator (`/` or `\`), an ASCII control character
    /// (including NUL), or is exactly `.` or `..`. `robot_id` is never used in
    /// SQL -- every query is parameterized -- but it does become a path
    /// segment in `storage_path` and an object-key component in
    /// `manifest_object_key` (`format!("manifests/{}/{}.json", robot_id,
    /// upload_id)`), so nothing that could act as a separator or a traversal
    /// step may reach either. Letters, digits, spaces, embedded dots, other
    /// punctuation and non-ASCII text are otherwise unrestricted.
    InvalidCharset,
    /// Well-formed, but no `robots` row has this id.
    NotRegistered,
}

impl RobotIdError {
    /// Short, stable snake_case label for the rejection metric.
    fn label(&self) -> &'static str {
        match self {
            RobotIdError::Missing => "missing",
            RobotIdError::UnknownSentinel => "unknown_sentinel",
            RobotIdError::TooLong => "too_long",
            RobotIdError::InvalidCharset => "invalid_charset",
            RobotIdError::NotRegistered => "not_registered",
        }
    }

    fn message(&self, robot_id: &str) -> String {
        match self {
            RobotIdError::Missing => {
                "missing robot_id: set `robot_id` in the agent's vault config".to_string()
            }
            RobotIdError::UnknownSentinel => {
                "robot_id is the literal \"unknown\" placeholder: set `robot_id` in the agent's vault config"
                    .to_string()
            }
            RobotIdError::TooLong => format!(
                "robot_id is {} characters; the maximum is {}",
                robot_id.chars().count(),
                MAX_ROBOT_ID_LEN
            ),
            // Do not interpolate the raw wire value here: this branch includes
            // control-bearing ids, which must not be reflected into gRPC errors
            // (and from there into client logs or terminals).
            RobotIdError::InvalidCharset =>
                "robot_id must not contain '/', '\\', ASCII control characters, or be '.' or '..': \
                 set `robot_id` in the agent's vault config"
                    .to_string(),
            RobotIdError::NotRegistered => format!(
                "robot_id '{}' is not registered in the fleet registry; add it on the Robots \
                 page (or, on a development stack, POST /admin/robots?robot_id={}) and retry",
                robot_id, robot_id
            ),
        }
    }
}

/// `robots.id` / `uploads.robot_id` are both VARCHAR(50)
/// (`db/migrations/001_baseline.sql`).
const MAX_ROBOT_ID_LEN: usize = 50;

/// A placeholder id that a client may send when it could not derive a robot id.
const UNKNOWN_ROBOT_SENTINEL: &str = "unknown";

/// Pure, DB-free half of the check, so `mod tests` can pin the shape rules
/// without a live Postgres. Returns `Ok(trimmed_id)` for an id that is worth
/// looking up at all.
fn validate_robot_id(robot_id: &str) -> Result<&str, RobotIdError> {
    // Check the raw wire value before trimming. Otherwise a trailing newline or
    // tab would be normalized away even though ASCII controls are forbidden.
    if robot_id.chars().any(|c| c.is_ascii_control()) {
        return Err(RobotIdError::InvalidCharset);
    }
    let trimmed = robot_id.trim();
    if trimmed.is_empty() {
        return Err(RobotIdError::Missing);
    }
    if trimmed.eq_ignore_ascii_case(UNKNOWN_ROBOT_SENTINEL) {
        return Err(RobotIdError::UnknownSentinel);
    }
    // Postgres counts characters, not bytes, for VARCHAR(n).
    if trimmed.chars().count() > MAX_ROBOT_ID_LEN {
        return Err(RobotIdError::TooLong);
    }
    // `robot_id` is never used in SQL -- every query is parameterized -- so
    // the only real risk is object-key/path construction: `storage_path`'s
    // `.join(robot_id)` and `manifest_object_key`'s
    // `format!("manifests/{}/{}.json", robot_id, upload_id)`.
    // `path_to_s3_key` also normalizes `\` to `/`, so both slash directions
    // are separators. Reject only what could act as a separator, a control
    // character, or a traversal step -- letters, spaces, ':' and non-ASCII
    // text are all fine here, and are already in real use (see
    // `robot_spiffe_uri_decodes_one_canonical_path_segment`'s "rover seven").
    let has_path_separator = trimmed.chars().any(|c| c == '/' || c == '\\');
    if has_path_separator || matches!(trimmed, "." | "..") {
        return Err(RobotIdError::InvalidCharset);
    }
    Ok(trimmed)
}

/// Map a refusal to the gRPC status returned to the client, and count it.
fn robot_id_status(err: RobotIdError, robot_id: &str) -> Status {
    counter!("ingest_robot_rejected_total", "reason" => err.label()).increment(1);
    Status::failed_precondition(err.message(robot_id))
}

/// Convert every fleet-registry query failure into a closed gate. A missing
/// migration deserves a more actionable message, but never authorization to
/// upload an unchecked robot identity.
fn robot_registry_query_error(sqlstate: Option<&str>) -> Status {
    if sqlstate == Some("42P01") {
        Status::unavailable(
            "robot_registry_schema_missing: apply database migrations before accepting uploads",
        )
    } else {
        Status::unavailable("robot_registry_unavailable")
    }
}

/// Resolve the canonical robot id **and** its lifecycle row, refusing the RPC
/// unless `robot_id` names a registered robot.
///
/// Called at the very top of both upload RPCs, before S3 or any `uploads`
/// write. A Postgres failure here is `Status::unavailable`, NOT an allow: this
/// is an authorization-shaped check, and failing open on a pool error would
/// make "the database is down" the way to bypass it.
///
/// `validate_robot_id` trims, so the id the registry is checked against can
/// differ from the raw wire value. Callers must use the returned form for the
/// S3 key, the generated upload_id and every `uploads` write -- otherwise
/// " demo " passes the registry check as `demo` but is stored untrimmed,
/// splitting one robot's data across two identities.
async fn lookup_upload_target(
    state: &AppState,
    robot_id: &str,
) -> Result<(String, RobotRegistration), Status> {
    let id = validate_robot_id(robot_id).map_err(|e| robot_id_status(e, robot_id))?;
    match robot_auth::lookup_robot_registration(&state.pg, &state.robot_registry, id).await {
        Ok(Some(registration)) => Ok((id.to_string(), registration)),
        Ok(None) => Err(robot_id_status(RobotIdError::NotRegistered, id)),
        Err((sqlstate, detail)) => {
            error!(
                "robot_registry_query_failed: robot_id={} sqlstate={:?} detail={}",
                id, sqlstate, detail
            );
            Err(robot_registry_query_error(sqlstate.as_deref()))
        }
    }
}

/// Gate an upload on the registry, the robot's lifecycle state, and — for the
/// shared operator token — the shared-token policy.
///
/// The two refusals here are deliberately different statuses, and the ordering
/// is deliberate too:
///
/// * A robot whose `state` is not `enrolled` is `FAILED_PRECONDITION`.
///   It is a statement about the *target*, and it applies to
///   the operator class as well, which has no robot state of its own.
/// * A robot **credential** on a non-enrolled robot never reaches this point:
///   `resolve_request_identity` already refused it with `UNAUTHENTICATED`.
///   Authentication runs before authorization, so a
///   disabled robot's own token gets the authentication answer, not this one.
async fn authorize_upload_target(
    state: &AppState,
    identity: &RequestIdentity,
    declared_robot_id: &str,
) -> Result<String, Status> {
    let (id, registration) = lookup_upload_target(state, declared_robot_id).await?;
    if !registration.is_enrolled() {
        counter!("ingest_robot_rejected_total", "reason" => "robot_not_enrolled").increment(1);
        return Err(Status::failed_precondition(format!(
            "robot_not_enrolled: robot_id={} is {} and may not upload",
            id, registration.state
        )));
    }
    // Only `allow` (the dev profile) accepts the shared token on
    // the data plane. The operator class is the only one this applies to — a
    // per-robot credential is by definition not the shared token.
    if matches!(identity.class, CredentialClass::Operator)
        && state.shared_token_policy != SharedTokenPolicy::Allow
    {
        counter!("ingest_auth_rejected_total", "reason" => "shared_token_policy").increment(1);
        return Err(Status::unauthenticated("invalid ingest auth token"));
    }
    Ok(id)
}

// ========== Release robot certificate identity =============================
//
// A trusted client-auth CA proves only that a certificate belongs to *some*
// member of the fleet.  It does not prove that the holder is the robot named in
// `UploadMetadata.robot_id`.  Release mode closes that gap with one explicit,
// portable identity contract:
//
//   URI:spiffe://robot-vault/robots/<percent-encoded robot_id>
//
// The leaf certificate must contain exactly one URI SAN under that trust domain
// and path.  CN is deliberately not a fallback: SAN is unambiguous, survives
// changes in subject formatting, and keeps authorization identity separate
// from a human-readable subject.  Tonic exposes the rustls-authenticated leaf
// DER on the request, so the comparison happens before the registry lookup or
// any object/database write.  The shared bearer token remains independently
// enforced by the service interceptor.

const ROBOT_SPIFFE_URI_PREFIX: &str = "spiffe://robot-vault/robots/";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CertificateRobotIdError {
    MissingPeerCertificate,
    MalformedCertificate,
    MissingRobotUriSan,
    AmbiguousRobotUriSan,
    MalformedRobotUriSan,
}

impl CertificateRobotIdError {
    fn label(self) -> &'static str {
        match self {
            Self::MissingPeerCertificate => "missing_peer_certificate",
            Self::MalformedCertificate => "malformed_certificate",
            Self::MissingRobotUriSan => "missing_robot_uri_san",
            Self::AmbiguousRobotUriSan => "ambiguous_robot_uri_san",
            Self::MalformedRobotUriSan => "malformed_robot_uri_san",
        }
    }
}

fn valid_percent_escapes(value: &str) -> bool {
    let bytes = value.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            if i + 2 >= bytes.len()
                || !bytes[i + 1].is_ascii_hexdigit()
                || !bytes[i + 2].is_ascii_hexdigit()
            {
                return false;
            }
            i += 3;
        } else {
            i += 1;
        }
    }
    true
}

/// Return a robot id only for a URI in Robot Vault's certificate namespace.
/// Other URI SANs (for example an inventory URI) are ignored.  A matching URI
/// is strict: one path segment, no query/fragment, valid percent escapes and a
/// decoded id accepted by the same validation as upload metadata.
fn robot_id_from_spiffe_uri(uri: &str) -> Result<Option<String>, CertificateRobotIdError> {
    let Some(encoded_id) = uri.strip_prefix(ROBOT_SPIFFE_URI_PREFIX) else {
        return Ok(None);
    };
    if encoded_id.is_empty()
        || encoded_id.contains('/')
        || encoded_id.contains('?')
        || encoded_id.contains('#')
        || !valid_percent_escapes(encoded_id)
    {
        return Err(CertificateRobotIdError::MalformedRobotUriSan);
    }
    let decoded = percent_decode_str(encoded_id)
        .decode_utf8()
        .map_err(|_| CertificateRobotIdError::MalformedRobotUriSan)?;
    let canonical =
        validate_robot_id(&decoded).map_err(|_| CertificateRobotIdError::MalformedRobotUriSan)?;
    Ok(Some(canonical.to_string()))
}

fn robot_id_from_certificate_der(der: &[u8]) -> Result<String, CertificateRobotIdError> {
    let (_, certificate) =
        parse_x509_certificate(der).map_err(|_| CertificateRobotIdError::MalformedCertificate)?;
    let san = certificate
        .subject_alternative_name()
        .map_err(|_| CertificateRobotIdError::MalformedCertificate)?
        .ok_or(CertificateRobotIdError::MissingRobotUriSan)?;
    let mut matched: Option<String> = None;
    for general_name in &san.value.general_names {
        if let GeneralName::URI(uri) = general_name {
            if let Some(robot_id) = robot_id_from_spiffe_uri(uri)? {
                if matched.is_some() {
                    return Err(CertificateRobotIdError::AmbiguousRobotUriSan);
                }
                matched = Some(robot_id);
            }
        }
    }
    matched.ok_or(CertificateRobotIdError::MissingRobotUriSan)
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn request_robot_certificate_identity<T>(
    request: &Request<T>,
    required: bool,
) -> Result<Option<String>, Status> {
    if !required {
        return Ok(None);
    }
    let result = request
        .peer_certs()
        .and_then(|certificates| certificates.first().cloned())
        .ok_or(CertificateRobotIdError::MissingPeerCertificate)
        .and_then(|certificate| robot_id_from_certificate_der(certificate.as_ref()));
    match result {
        Ok(robot_id) => Ok(Some(robot_id)),
        Err(error) => {
            counter!("ingest_robot_identity_rejected_total", "reason" => error.label())
                .increment(1);
            Err(Status::unauthenticated(format!(
                "release client certificate identity rejected: {} (expected URI SAN {}<percent-encoded-robot-id>)",
                error.label(), ROBOT_SPIFFE_URI_PREFIX
            )))
        }
    }
}

/// Refuse an upload whose declared `robot_id` is not the authenticated robot.
///
/// **This is the whole point of `AuthenticatedRobot`.** It has two producers
/// (the mTLS SPIFFE SAN and a per-robot bearer), so the same code scopes a
/// per-robot bearer exactly as it scopes a client certificate. `None` means
/// "no robot identity at all", which is the operator class and the development
/// behaviour.
#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn enforce_robot_certificate_claim(
    authenticated: Option<&AuthenticatedRobot>,
    declared_robot_id: &str,
) -> Result<(), Status> {
    let Some(authenticated) = authenticated else {
        return Ok(());
    };
    let declared = validate_robot_id(declared_robot_id)
        .map_err(|error| robot_id_status(error, declared_robot_id))?;
    if authenticated.robot_id() != declared {
        counter!(
            "ingest_robot_identity_rejected_total",
            "reason" => "claim_mismatch",
            "source" => authenticated.source.label()
        )
        .increment(1);
        return Err(Status::permission_denied(
            "authenticated robot identity does not match UploadMetadata.robot_id",
        ));
    }
    Ok(())
}

/// Resolve the robot filter a caller is authorized to use.
///
/// When an authenticated robot is present — from a client certificate under
/// `mtls`, or from a per-robot bearer under any profile — an omitted
/// filter is narrowed to that robot and an explicit cross-robot filter is
/// refused. `None` (operator class) keeps the optional filter.
///
/// The `None` branch returning "no filter, list the whole fleet" is correct for
/// the operator token only; a robot bearer must never reach it, or it could
/// read the whole fleet.
#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn authorized_robot_filter(
    authenticated: Option<&AuthenticatedRobot>,
    requested_robot_id: &str,
) -> Result<Option<String>, Status> {
    let Some(authenticated) = authenticated else {
        return Ok((!requested_robot_id.is_empty()).then(|| requested_robot_id.to_string()));
    };
    if !requested_robot_id.is_empty() {
        enforce_robot_certificate_claim(Some(authenticated), requested_robot_id)?;
    }
    Ok(Some(authenticated.robot_id().to_string()))
}

/// Authorize access to an upload row whose owner was read from Postgres.
/// An authenticated robot must own the row; the operator class retains the
/// existing shared-token behaviour.
#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn enforce_robot_resource_owner(
    authenticated: Option<&AuthenticatedRobot>,
    row_robot_id: &str,
) -> Result<(), Status> {
    let Some(authenticated) = authenticated else {
        return Ok(());
    };
    if authenticated.robot_id() != row_robot_id {
        counter!(
            "ingest_robot_identity_rejected_total",
            "reason" => "resource_owner_mismatch",
            "source" => authenticated.source.label()
        )
        .increment(1);
        return Err(Status::permission_denied(
            "authenticated robot identity does not own the requested upload",
        ));
    }
    Ok(())
}

/// What one request presented, lifted out of the `Request` so the asynchronous
/// credential lookup does not have to hold a borrow of it.
///
/// That is not a style choice: `tonic::Streaming` is `Send` but not `Sync`, so
/// a `&Request<Streaming<_>>` held across an `.await` makes the whole handler
/// future non-`Send` and the service stops compiling. Extracting first keeps
/// the streaming call site identical in shape to the four unary ones.
struct PresentedCredentials {
    certificate_robot_id: Option<String>,
    /// The raw `authorization` value — a secret, and therefore never rendered
    /// by `Debug`. `derive(Debug)` here would put a live robot credential into
    /// the panic message of any `expect` that touches this struct.
    authorization: Option<String>,
}

impl std::fmt::Debug for PresentedCredentials {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PresentedCredentials")
            .field("certificate_robot_id", &self.certificate_robot_id)
            .field(
                "authorization",
                &self.authorization.as_ref().map(|_| "<redacted>"),
            )
            .finish()
    }
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn extract_presented_credentials<T>(
    request: &Request<T>,
    require_robot_certificate_identity: bool,
) -> Result<PresentedCredentials, Status> {
    Ok(PresentedCredentials {
        certificate_robot_id: request_robot_certificate_identity(
            request,
            require_robot_certificate_identity,
        )?,
        authorization: request
            .metadata()
            .get(AUTH_HEADER)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string),
    })
}

/// Turn one request's `authorization` metadata and its
/// (optional) client certificate into the identity the five ownership call
/// sites consume.
///
/// Ordering is the contract:
///
/// 1. The certificate identity is resolved first, because under `mtls` a
///    missing one must refuse the RPC rather than fall through to
///    `authorized_robot_filter(None, …)`'s fleet-wide behaviour.
/// 2. A presented value equal to `INGEST_AUTH_TOKEN` is the operator class and
///    carries no robot identity of its own.
/// 3. Anything else that has the shape of `ignr1.…` is looked up in
///    `robot_credentials`. Failure is `UNAUTHENTICATED "invalid ingest auth
///    token"` — byte-identical to what a garbage token gets from the
///    interceptor, so a well-formed unknown credential is not distinguishable
///    from noise.
/// 4. When both a certificate and a credential are present they must name the
///    same robot.
async fn resolve_request_identity(
    state: &AppState,
    presented: PresentedCredentials,
) -> Result<RequestIdentity, Status> {
    let PresentedCredentials {
        certificate_robot_id,
        authorization,
    } = presented;
    let presented = authorization.as_deref();
    let shared_outcome = evaluate_auth(state.auth_token.as_deref(), presented);
    let auth_disabled = state
        .auth_token
        .as_deref()
        .map(str::trim)
        .is_none_or(str::is_empty);

    // Class `operator`, or auth disabled entirely (dev only — `token` and
    // `mtls` both require the token at startup).
    if shared_outcome == AuthOutcome::Allowed {
        return Ok(RequestIdentity {
            class: if auth_disabled {
                CredentialClass::AuthDisabled
            } else {
                CredentialClass::Operator
            },
            robot: certificate_robot_id.map(AuthenticatedRobot::from_certificate),
            credential_id: None,
            credential_sha256: None,
        });
    }

    let bearer = presented.map(strip_bearer_scheme).unwrap_or_default();
    let Some(token_sha256) = robot_auth::parse_robot_credential(bearer) else {
        // The interceptor already rejects this shape; reaching here means a
        // handler was called without one, so refuse identically.
        counter!("ingest_auth_rejected_total", "reason" => "invalid").increment(1);
        return Err(Status::unauthenticated("invalid ingest auth token"));
    };
    let verified = match robot_auth::verify_robot_credential(
        &state.pg,
        &state.credential_cache,
        &token_sha256,
    )
    .await
    {
        Ok(Ok(verified)) => verified,
        Ok(Err(rejection)) => {
            counter!(
                "ingest_auth_rejected_total",
                "reason" => "invalid",
                "detail" => rejection.label()
            )
            .increment(1);
            return Err(Status::unauthenticated("invalid ingest auth token"));
        }
        Err(detail) => {
            // An authorization-shaped check must never fail open on a pool
            // or query error.
            warn!(%detail, "per-robot credential verification unavailable");
            return Err(Status::unavailable("robot_credential_registry_unavailable"));
        }
    };

    // With mTLS also on, the SPIFFE id and the credential's robot
    // id must agree before either is trusted.
    if let Some(certificate_robot_id) = certificate_robot_id.as_deref() {
        if certificate_robot_id != verified.robot_id {
            counter!("ingest_robot_identity_rejected_total", "reason" => "identity_source_conflict")
                .increment(1);
            return Err(Status::permission_denied(
                "client certificate robot identity does not match the presented robot credential",
            ));
        }
    }
    Ok(RequestIdentity {
        class: CredentialClass::Robot,
        robot: Some(AuthenticatedRobot::from_bearer(verified.robot_id.clone())),
        credential_id: Some(verified.credential_id),
        credential_sha256: Some(token_sha256),
    })
}

/// Re-verify a long-lived stream's credential and its robot's state.
///
/// Returns the reason label when the stream must be aborted. The credential
/// cache's own 30 s TTL is what bounds the staleness, so this costs one indexed
/// lookup per 30 s per active stream and no more.
async fn recheck_stream_identity(
    state: &AppState,
    identity: &RequestIdentity,
    robot_id: &str,
) -> Option<&'static str> {
    if let Some(token_sha256) = identity.credential_sha256.as_ref() {
        match robot_auth::verify_robot_credential(&state.pg, &state.credential_cache, token_sha256)
            .await
        {
            Ok(Ok(verified)) if verified.robot_id == robot_id => {}
            Ok(Ok(_)) => return Some("credential_robot_mismatch"),
            Ok(Err(rejection)) => return Some(rejection.label()),
            // A database blip must not kill a healthy multi-gigabyte upload;
            // the next interval retries, and the interceptor already proved the
            // credential live when the stream opened.
            Err(detail) => {
                warn!(%detail, "mid-stream credential re-check unavailable");
                return None;
            }
        }
        return None;
    }
    // Operator class: there is no credential to re-check, but the robot can
    // still be disabled mid-stream, which is the main operator control here.
    match robot_auth::lookup_robot_registration(&state.pg, &state.robot_registry, robot_id).await {
        Ok(Some(registration)) if registration.is_enrolled() => None,
        Ok(Some(_)) => Some("robot_not_enrolled"),
        Ok(None) => Some("robot_not_registered"),
        Err((_, detail)) => {
            warn!(%detail, "mid-stream registry re-check unavailable");
            None
        }
    }
}

/// What the synchronous gRPC interceptor may conclude on its own.
///
/// The interceptor cannot do the `robot_credentials` lookup — it is not async —
/// so the credential check is split. Keeping the decision in a pure function
/// lets the cross-class presentation checks on the data plane be unit tests
/// rather than a live-server exercise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum InterceptorDecision {
    /// The shared operator token, or auth is disabled entirely.
    Pass,
    /// Shape-valid `ignr1.…`: the handler resolves it against the database and
    /// refuses an unknown one with the *same* message this layer would have
    /// used, so deferring is not observable.
    DeferToHandler,
    Reject(AuthOutcome),
}

fn interceptor_decision(expected: Option<&str>, presented: Option<&str>) -> InterceptorDecision {
    match evaluate_auth(expected, presented) {
        AuthOutcome::Allowed => InterceptorDecision::Pass,
        AuthOutcome::InvalidCredential
            if presented
                .map(strip_bearer_scheme)
                .and_then(robot_auth::parse_robot_credential)
                .is_some() =>
        {
            InterceptorDecision::DeferToHandler
        }
        rejected => InterceptorDecision::Reject(rejected),
    }
}

/// Strip an optional case-insensitive `Bearer ` prefix, matching `evaluate_auth`.
fn strip_bearer_scheme(presented: &str) -> &str {
    let presented = presented.trim();
    let (scheme, rest) = presented
        .split_once(char::is_whitespace)
        .unwrap_or((presented, ""));
    if scheme.eq_ignore_ascii_case(BEARER_SCHEME) {
        rest.trim()
    } else {
        presented
    }
}

// ========== Immutable upload-id reservation ================================

const UPLOAD_ADVISORY_LOCK_SQL: &str = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))";
const UPLOAD_ADVISORY_UNLOCK_SQL: &str = "SELECT pg_advisory_unlock(hashtextextended($1, 0))";

/// A cross-process lock held from the reservation transaction until this RPC
/// has either completed its database row or abandoned the attempt.  This is a
/// Postgres *session* advisory lock, so it also serializes multiple ingest
/// replicas.  `Drop` schedules the unlock on every `?`, cancellation and early
/// return path; closing the connection is a final backstop if the process dies.
struct UploadAdvisoryLock {
    client: Option<Object>,
    upload_id: String,
}

impl UploadAdvisoryLock {
    fn new(client: Object, upload_id: &str) -> Self {
        Self {
            client: Some(client),
            upload_id: upload_id.to_string(),
        }
    }

    fn client_mut(&mut self) -> &mut Object {
        self.client
            .as_mut()
            .expect("upload advisory-lock client present")
    }

    async fn release(mut self) {
        if let Some(client) = self.client.take() {
            if let Err(error) = client
                .execute(UPLOAD_ADVISORY_UNLOCK_SQL, &[&self.upload_id])
                .await
            {
                warn!(upload_id = %self.upload_id, %error, "upload advisory unlock failed");
                // Never return a possibly still-locked session to the pool.
                // Taking the managed object out of deadpool makes the
                // underlying Postgres client close when it is dropped, which
                // releases every session advisory lock server-side.
                drop(Object::take(client));
            }
        }
    }
}

impl Drop for UploadAdvisoryLock {
    fn drop(&mut self) {
        let Some(client) = self.client.take() else {
            return;
        };
        let upload_id = self.upload_id.clone();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                if let Err(error) = client
                    .execute(UPLOAD_ADVISORY_UNLOCK_SQL, &[&upload_id])
                    .await
                {
                    warn!(%upload_id, %error, "upload advisory unlock failed during cleanup");
                    // An unlock error must not put a session that might still
                    // own the lock back into the pool.
                    drop(Object::take(client));
                }
            });
        } else {
            // No executor is available to issue the unlock (normally only
            // during runtime teardown), so close instead of pooling the
            // session while it may still own an advisory lock.
            drop(Object::take(client));
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ImmutableUploadIdentity {
    client_upload_id: String,
    robot_id: String,
    filename: String,
    file_size_bytes: i64,
    expected_sha256: String,
}

#[derive(Debug, Clone)]
enum UploadReservationOutcome {
    Writable { storage_path: String },
    AlreadyCompleted(UploadResponse),
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn normalize_sha256(value: &str) -> Result<String, Status> {
    let normalized = value.trim().to_ascii_lowercase();
    if normalized.len() != 64 || !normalized.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(Status::invalid_argument(
            "expected_sha256 must contain exactly 64 hexadecimal characters",
        ));
    }
    Ok(normalized)
}

/// Resolve the client-declared digest for UploadSmall and require that it
/// matches the received bytes. Every profile requires this source-side claim:
/// a digest computed only after bytes arrive proves storage consistency, not
/// that the received bytes equal the robot's source file.
#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn resolve_unary_expected_sha256(raw_expected: &str, final_hash: &str) -> Result<String, Status> {
    if raw_expected.trim().is_empty() {
        return Err(Status::invalid_argument(
            "UploadSmall requires expected_sha256",
        ));
    }
    let expected = normalize_sha256(raw_expected)?;
    if expected != final_hash {
        return Err(Status::invalid_argument("sha256_mismatch"));
    }
    Ok(expected)
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn validate_upload_metadata_shape(
    upload_id: &str,
    filename: &str,
    file_size: i64,
) -> Result<(), Status> {
    if upload_id.is_empty()
        || upload_id.len() > 128
        || !upload_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    {
        return Err(Status::invalid_argument(
            "upload_id must be 1-128 ASCII letters, digits, '.', '_' or '-'",
        ));
    }
    if filename.is_empty() || filename.chars().count() > 255 || filename.contains('\0') {
        return Err(Status::invalid_argument(
            "filename must contain 1-255 characters and no NUL byte",
        ));
    }
    if file_size < 0 {
        return Err(Status::invalid_argument("file_size must be non-negative"));
    }
    Ok(())
}

fn immutable_identity_conflicts(
    reserved: &ImmutableUploadIdentity,
    attempted: &ImmutableUploadIdentity,
) -> Vec<&'static str> {
    let mut conflicts = Vec::new();
    if reserved.robot_id != attempted.robot_id {
        conflicts.push("robot_id");
    }
    if reserved.filename != attempted.filename {
        conflicts.push("filename");
    }
    if reserved.file_size_bytes != attempted.file_size_bytes {
        conflicts.push("file_size");
    }
    if reserved.expected_sha256 != attempted.expected_sha256 {
        conflicts.push("expected_sha256");
    }
    conflicts
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn parse_s3_storage_path(storage_path: &str) -> Result<(String, String), Status> {
    let value = storage_path
        .strip_prefix("s3://")
        .ok_or_else(|| Status::internal("reserved upload storage_path is not an s3:// URL"))?;
    let (bucket, key) = value
        .split_once('/')
        .ok_or_else(|| Status::internal("reserved upload storage_path has no object key"))?;
    if bucket.is_empty() || key.is_empty() {
        return Err(Status::internal(
            "reserved upload storage_path is incomplete",
        ));
    }
    Ok((bucket.to_string(), key.to_string()))
}

const RESERVE_UPLOAD_INSERT_SQL: &str = "INSERT INTO uploads (client_upload_id, robot_id, original_filename, storage_path, file_size_bytes, expected_sha256, content_type, status, upload_started_at, operator, site, permission_ref, session_tags, task_label) VALUES ($1, $2, $3, $4, $5, $6, $7, 'uploading', NOW(), $8, $9, $10, $11, $12) ON CONFLICT (client_upload_id) DO NOTHING";

/// Persist and verify the immutable idempotency tuple before the caller is
/// allowed to touch S3.  The advisory lock is acquired first and kept by the
/// returned guard through object I/O and the terminal database write.
#[allow(clippy::too_many_arguments)] // Parameters mirror one immutable reservation row.
async fn reserve_upload_identity(
    pg: &Pool,
    identity: &ImmutableUploadIdentity,
    proposed_storage_path: &str,
    content_type: &str,
    operator: &str,
    site: &str,
    permission_ref: &str,
    session_tags: &str,
    task_label: &str,
    allow_durable: bool,
) -> Result<(UploadAdvisoryLock, UploadReservationOutcome), Status> {
    let client = pg.get().await.map_err(|error| {
        warn!(%error, "upload reservation pool checkout failed");
        Status::unavailable("upload reservation database unavailable")
    })?;
    let lock_row = client
        .query_one(UPLOAD_ADVISORY_LOCK_SQL, &[&identity.client_upload_id])
        .await
        .map_err(|error| {
            warn!(upload_id = %identity.client_upload_id, %error, "upload advisory lock failed");
            Status::unavailable("upload reservation database unavailable")
        })?;
    if !lock_row.get::<_, bool>(0) {
        counter!("ingest_upload_reservation_rejected_total", "reason" => "already_in_progress")
            .increment(1);
        return Err(Status::aborted(
            "client_upload_id already has an upload in progress; retry after it completes",
        ));
    }
    let mut advisory_lock = UploadAdvisoryLock::new(client, &identity.client_upload_id);

    let operator = empty_to_null(operator);
    let site = empty_to_null(site);
    let permission_ref = empty_to_null(permission_ref);
    let session_tags = empty_to_null(session_tags);
    let task_label = empty_to_null(task_label);
    let transaction = advisory_lock.client_mut().transaction().await.map_err(|error| {
        warn!(upload_id = %identity.client_upload_id, %error, "upload reservation transaction start failed");
        Status::unavailable("upload reservation database unavailable")
    })?;
    transaction
        .execute(
            RESERVE_UPLOAD_INSERT_SQL,
            &[
                &identity.client_upload_id,
                &identity.robot_id,
                &identity.filename,
                &proposed_storage_path,
                &identity.file_size_bytes,
                &identity.expected_sha256,
                &content_type,
                &operator,
                &site,
                &permission_ref,
                &session_tags,
                &task_label,
            ],
        )
        .await
        .map_err(|error| {
            warn!(upload_id = %identity.client_upload_id, %error, "upload reservation insert failed");
            Status::unavailable("upload reservation database unavailable")
        })?;

    let row = transaction
        .query_one(
            "SELECT robot_id, original_filename, file_size_bytes, expected_sha256, status, sha256_hash, storage_path, upload_completed_at, bytes_received FROM uploads WHERE client_upload_id=$1 FOR UPDATE",
            &[&identity.client_upload_id],
        )
        .await
        .map_err(|error| {
            warn!(upload_id = %identity.client_upload_id, %error, "upload reservation readback failed");
            Status::unavailable("upload reservation database unavailable")
        })?;
    let reserved_identity = ImmutableUploadIdentity {
        client_upload_id: identity.client_upload_id.clone(),
        robot_id: row.get(0),
        filename: row.get(1),
        file_size_bytes: row.get(2),
        expected_sha256: row.get::<_, Option<String>>(3).unwrap_or_default(),
    };
    let conflicts = immutable_identity_conflicts(&reserved_identity, identity);
    if !conflicts.is_empty() {
        counter!("ingest_upload_reservation_rejected_total", "reason" => "identity_conflict")
            .increment(1);
        return Err(Status::already_exists(format!(
            "client_upload_id is already reserved with different immutable metadata: {}",
            conflicts.join(", ")
        )));
    }

    if !allow_durable && transaction.query_opt(
        "SELECT 1 FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id WHERE u.client_upload_id=$1",
        &[&identity.client_upload_id],
    ).await.map_err(|_| Status::unavailable("resume database unavailable"))?.is_some() {
        return Err(Status::failed_precondition("this upload has durable parts; resume it with streaming Upload"));
    }

    let status: String = row.get(4);
    let stored_hash: Option<String> = row.get(5);
    let storage_path: String = row.get(6);
    let completed_at: Option<chrono::DateTime<Utc>> = row.get(7);
    let bytes_received: Option<i64> = row.get(8);
    let outcome = if status == "completed" {
        let stored_hash = stored_hash
            .ok_or_else(|| Status::internal("completed upload reservation has no sha256_hash"))?;
        if !stored_hash.eq_ignore_ascii_case(&identity.expected_sha256) {
            counter!("ingest_upload_reservation_rejected_total", "reason" => "completed_hash_conflict").increment(1);
            return Err(Status::already_exists(
                "client_upload_id is already completed with a different digest",
            ));
        }
        let completed_at = completed_at.map(|value| Timestamp {
            seconds: value.timestamp(),
            nanos: value.timestamp_subsec_nanos() as i32,
        });
        UploadReservationOutcome::AlreadyCompleted(UploadResponse {
            upload_id: identity.client_upload_id.clone(),
            status,
            bytes_received: bytes_received.unwrap_or(identity.file_size_bytes),
            final_hash: stored_hash,
            completed_at,
            storage_path,
            error_message: String::new(),
        })
    } else {
        // A crashed/failed attempt left a durable reservation.  Once the
        // cross-process lock is ours, byte-identical metadata may restart it.
        transaction
            .execute(
                "UPDATE uploads SET status='uploading', error_message=NULL, upload_started_at=NOW(), upload_completed_at=NULL, bytes_received=0 WHERE client_upload_id=$1",
                &[&identity.client_upload_id],
            )
            .await
            .map_err(|error| {
                warn!(upload_id = %identity.client_upload_id, %error, "upload reservation restart failed");
                Status::unavailable("upload reservation database unavailable")
            })?;
        UploadReservationOutcome::Writable { storage_path }
    };
    transaction.commit().await.map_err(|error| {
        warn!(upload_id = %identity.client_upload_id, %error, "upload reservation commit failed");
        Status::unavailable("upload reservation database unavailable")
    })?;
    Ok((advisory_lock, outcome))
}

#[tonic::async_trait]
impl UploadService for IngestSvc {
    async fn upload_small(
        &self,
        request: Request<UploadSmallRequest>,
    ) -> Result<Response<UploadResponse>, Status> {
        // Ownership call site 1 of 5.
        let presented =
            extract_presented_credentials(&request, self.state.require_robot_certificate_identity)?;
        let request_identity = resolve_request_identity(&self.state, presented).await?;
        let req = request.into_inner();
        let meta = req.metadata.unwrap_or_default();
        let robot_id = meta.robot_id.clone();
        enforce_robot_certificate_claim(request_identity.robot(), &robot_id)?;
        // The fleet registry gate, now also the lifecycle-state and
        // shared-token-policy gate. Runs before the concurrency permits, before
        // S3, and before any `uploads` write, so an unregistered, disabled or
        // policy-refused robot costs nothing and leaves nothing behind.
        let robot_id = authorize_upload_target(&self.state, &request_identity, &robot_id).await?;
        // Fail fast at both in-handler gates. The transport admission wrapper
        // already owns a process-wide decode slot, so waiting here would let a
        // saturated robot occupy slots needed by other robots.
        let _robot_permit =
            try_acquire_per_robot(&self.state, &robot_id, self.state.per_robot_limit)?;
        let _global_permit = self
            .state
            .global_sem
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("global upload capacity exceeded"))?;

        let filename = meta.filename;
        let upload_id = if meta.upload_id.is_empty() {
            Uuid::new_v4().to_string()
        } else {
            meta.upload_id
        };
        let data = req.data;
        validate_upload_metadata_shape(&upload_id, &filename, meta.file_size)?;
        if meta.file_size != data.len() as i64 {
            return Err(Status::invalid_argument(
                "UploadSmall metadata.file_size does not match the request body length",
            ));
        }
        let mut hasher = Sha256::new();
        hasher.update(&data);
        let final_hash = hex::encode(hasher.finalize());
        // The robot's digest is required before any S3 or database side effect.
        let expected = resolve_unary_expected_sha256(&meta.expected_sha256, &final_hash)?;
        let s3 = self
            .state
            .s3_client
            .as_ref()
            .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
        let configured_bucket = self.state.s3_bucket.clone().unwrap_or_default();
        if configured_bucket.is_empty() {
            return Err(Status::unavailable("s3_not_configured"));
        }
        let dest = storage_path(&self.state.storage_root, &robot_id, &upload_id, &filename);
        let raw_key = path_to_s3_key(&dest);
        let file_sha_for_cas = expected.clone();
        let cas_key = format!("cas/files/{}", file_sha_for_cas);
        let proposed_key = if self.state.enable_file_cas {
            cas_key.as_str()
        } else {
            raw_key.as_str()
        };
        let proposed_storage_path = format!("s3://{}/{}", configured_bucket, proposed_key);
        let identity = ImmutableUploadIdentity {
            client_upload_id: upload_id.clone(),
            robot_id: robot_id.clone(),
            filename: filename.clone(),
            file_size_bytes: data.len() as i64,
            expected_sha256: expected.clone(),
        };
        let (reservation_lock, reservation_outcome) = reserve_upload_identity(
            &self.state.pg,
            &identity,
            &proposed_storage_path,
            meta.content_type.as_str(),
            &meta.operator,
            &meta.site,
            &meta.permission_ref,
            &meta.session_tags,
            &meta.task_label,
            false,
        )
        .await?;
        let authoritative_storage_path = match reservation_outcome {
            UploadReservationOutcome::AlreadyCompleted(response) => {
                counter!("ingest_upload_idempotent_replays_total", "method" => "upload_small")
                    .increment(1);
                reservation_lock.release().await;
                return Ok(Response::new(response));
            }
            UploadReservationOutcome::Writable { storage_path } => storage_path,
        };
        let (bucket, key) = parse_s3_storage_path(&authoritative_storage_path)?;
        let use_cas = key.starts_with("cas/files/");

        // Storage: S3 only (optional file-level CAS for small uploads), and only
        // after Postgres committed the immutable reservation above.
        let write_start = std::time::Instant::now();
        if use_cas {
            ensure_cas_upload_artifacts(
                &self.state.pg,
                s3,
                &bucket,
                &key,
                &upload_id,
                &robot_id,
                &filename,
                data.len() as i64,
                &file_sha_for_cas,
                &authoritative_storage_path,
                data.clone(),
            )
            .await?;
        } else {
            s3.put_object()
                .bucket(&bucket)
                .key(&key)
                .body(ByteStream::from(data.clone()))
                .send()
                .await
                .map_err(|e| {
                    warn!("s3_put_error_detail: {:?}", e);
                    Status::internal(format!("s3_put_error: {}", e))
                })?;
        }
        histogram!("ingest_write_seconds", "method" => "upload_small")
            .record(write_start.elapsed().as_secs_f64());

        let status = "completed".to_string();
        let resp = UploadResponse {
            upload_id: upload_id.clone(),
            status: status.clone(),
            bytes_received: data.len() as i64,
            final_hash: final_hash.clone(),
            completed_at: now_ts(),
            storage_path: authoritative_storage_path,
            error_message: String::new(),
        };

        // The completed `uploads` row is the ONLY signal the processor uses
        // to discover this object. Write it FIRST and treat a failure as a failed
        // upload: surface Status::internal so the client retries, rather than
        // reporting `completed` and stranding the S3 object invisibly forever.
        // Whole-file unary upload: the client-declared digest matched above.
        let completed_row_write = pg_insert_completed_upload(
            &self.state.pg,
            &upload_id,
            &robot_id,
            &filename,
            data.len() as i64,
            &resp.storage_path,
            &final_hash,
            meta.content_type.as_str(),
            &meta.operator,
            &meta.site,
            &meta.permission_ref,
            &meta.session_tags,
            &meta.task_label,
        )
        .await;
        if let Err(ref e) = completed_row_write {
            error!(
                "completed_row_write_failed: method=upload_small upload_id={} err={}",
                upload_id, e
            );
            if self.state.include_robot_labels {
                counter!("completed_row_write_failures_total", "method" => "upload_small", "robot_id" => robot_id.clone()).increment(1);
            } else {
                counter!("completed_row_write_failures_total", "method" => "upload_small")
                    .increment(1);
            }
        }
        // Only once the completed row is confirmed do we return the `completed`
        // response and announce success downstream.  In CAS mode the
        // upload-owned manifest registry row and JSON object were both made
        // durable before this terminal write.
        let resp = finalize_completion(resp, completed_row_write.map_err(|e| e.to_string()))?;

        if self.state.include_robot_labels {
            counter!("uploads_completed", "method" => "upload_small", "robot_id" => robot_id.clone()).increment(1);
        } else {
            counter!("uploads_completed", "method" => "upload_small").increment(1);
        }
        reservation_lock.release().await;
        Ok(Response::new(resp))
    }

    // No server streaming type required for these methods

    async fn upload(
        &self,
        request: Request<tonic::Streaming<UploadRequest>>,
    ) -> Result<Response<UploadResponse>, Status> {
        // Ownership call site 2 of 5.
        let presented =
            extract_presented_credentials(&request, self.state.require_robot_certificate_identity)?;
        let request_identity = resolve_request_identity(&self.state, presented).await?;
        let mut stream = request.into_inner();
        // First message must contain metadata. Transport admission is already
        // held here, so the timeout prevents an idle stream retaining one of
        // those process-wide pre-decode slots indefinitely.
        let first = tokio::time::timeout(self.state.first_message_timeout, stream.next())
            .await
            .map_err(|_| Status::deadline_exceeded("upload metadata timed out"))?
            .ok_or_else(|| Status::invalid_argument("missing first metadata message"))??;
        let meta = match first.data {
            Some(pb::upload_request::Data::Metadata(m)) => m,
            _ => return Err(Status::invalid_argument("first message must be metadata")),
        };
        let robot_id = meta.robot_id.clone();
        enforce_robot_certificate_claim(request_identity.robot(), &robot_id)?;
        // The same fleet-registry gate as `upload_small`, applied as soon as
        // the metadata message lands — before the MPU is created and before
        // `reserve_upload_identity`, so an unregistered robot never opens a
        // multipart upload that GC would later have to reclaim.
        let robot_id = authorize_upload_target(&self.state, &request_identity, &robot_id).await?;
        resumable::upload(&self.state, stream, meta, request_identity, robot_id).await
    }

    async fn get_upload_status(
        &self,
        request: Request<pb::UploadStatusRequest>,
    ) -> Result<Response<pb::UploadStatusResponse>, Status> {
        // Ownership call site 3 of 5.
        let presented =
            extract_presented_credentials(&request, self.state.require_robot_certificate_identity)?;
        let identity = resolve_request_identity(&self.state, presented).await?;
        let scoped = identity.robot().is_some();
        let upload_id = request.into_inner().upload_id;
        let upload_id_clone = upload_id.clone();
        // Postgres-only status lookup
        let pg = self.state.pg.clone();
        match pg.get().await {
            Ok(client) => {
                let row = match client.query_opt(
                    "SELECT status, bytes_received, file_size_bytes, EXTRACT(epoch FROM upload_started_at)::int, EXTRACT(epoch FROM COALESCE(updated_at, upload_completed_at))::int, robot_id FROM uploads WHERE client_upload_id=$1 LIMIT 1",
                    &[&upload_id],
                ).await {
                    Ok(row) => row,
                    Err(error) if scoped => {
                        warn!(%error, %upload_id, "scoped upload status ownership lookup failed");
                        return Err(Status::unavailable("upload status database unavailable"));
                    }
                    // Preserve development's historical unknown-on-query-error
                    // behavior. A scoped caller cannot do this because it must
                    // prove ownership before returning row data.
                    Err(_) => None,
                };
                if let Some(r) = row {
                    let row_robot_id: String = r.get(5);
                    enforce_robot_resource_owner(identity.robot(), &row_robot_id)?;
                    let status: String = r.get(0);
                    let br: i64 = r.get(1);
                    let total: i64 = r.get(2);
                    let started: i32 = r.get(3);
                    let updated: i32 = r.get(4);
                    return Ok(Response::new(pb::UploadStatusResponse {
                        upload_id: upload_id_clone,
                        status,
                        bytes_received: br,
                        total_bytes: total,
                        started_at: Some(Timestamp {
                            seconds: started as i64,
                            nanos: 0,
                        }),
                        updated_at: Some(Timestamp {
                            seconds: updated as i64,
                            nanos: 0,
                        }),
                        error_message: String::new(),
                    }));
                }
            }
            Err(error) if scoped => {
                warn!(%error, %upload_id, "scoped upload status pool checkout failed");
                return Err(Status::unavailable("upload status database unavailable"));
            }
            Err(_) => {}
        }
        Ok(Response::new(pb::UploadStatusResponse {
            upload_id: upload_id_clone,
            status: "unknown".into(),
            bytes_received: 0,
            total_bytes: 0,
            started_at: None,
            updated_at: None,
            error_message: String::new(),
        }))
    }

    async fn list_uploads(
        &self,
        request: Request<pb::ListUploadsRequest>,
    ) -> Result<Response<pb::ListUploadsResponse>, Status> {
        // Ownership call site 4 of 5 — the fleet-read containment gate.
        let presented =
            extract_presented_credentials(&request, self.state.require_robot_certificate_identity)?;
        let identity = resolve_request_identity(&self.state, presented).await?;
        let scoped = identity.robot().is_some();
        let req = request.into_inner();
        let robot_filter = authorized_robot_filter(identity.robot(), &req.robot_id)?;
        let page_size = if req.page_size <= 0 {
            20
        } else {
            req.page_size.min(100)
        } as i64;
        let pg = self.state.pg.clone();
        match pg.get().await {
            Ok(client) => {
                let rows_result = if let Some(rid) = robot_filter.as_ref() {
                    client.query(
                        "SELECT client_upload_id, robot_id, original_filename, file_size_bytes, status, storage_path, EXTRACT(epoch FROM upload_started_at)::int, EXTRACT(epoch FROM upload_completed_at)::int FROM uploads WHERE robot_id=$1 ORDER BY upload_completed_at DESC NULLS LAST LIMIT $2",
                        &[rid, &page_size]
                    ).await
                } else {
                    client.query(
                        "SELECT client_upload_id, robot_id, original_filename, file_size_bytes, status, storage_path, EXTRACT(epoch FROM upload_started_at)::int, EXTRACT(epoch FROM upload_completed_at)::int FROM uploads ORDER BY upload_completed_at DESC NULLS LAST LIMIT $1",
                        &[&page_size]
                    ).await
                };
                let rows = match rows_result {
                    Ok(rows) => rows,
                    Err(error) if scoped => {
                        warn!(%error, "scoped upload list query failed");
                        return Err(Status::unavailable("upload list database unavailable"));
                    }
                    // Preserve development's historical empty-list behavior
                    // when its query fails.
                    Err(_) => Vec::new(),
                };
                let mut items: Vec<pb::UploadInfo> = Vec::new();
                for r in rows {
                    let upload_id: String = r.get(0);
                    let robot_id: String = r.get(1);
                    let filename: String = r.get(2);
                    let file_size: i64 = r.get(3);
                    let status: String = r.get(4);
                    let storage_path: String = r.get(5);
                    let created_at: i32 = r.get(6);
                    let completed_at: Option<i32> = r.get(7);
                    items.push(pb::UploadInfo {
                        upload_id,
                        robot_id,
                        filename,
                        file_size,
                        status,
                        created_at: Some(Timestamp {
                            seconds: created_at as i64,
                            nanos: 0,
                        }),
                        completed_at: completed_at.map(|s| Timestamp {
                            seconds: s as i64,
                            nanos: 0,
                        }),
                        storage_path,
                    });
                }
                return Ok(Response::new(pb::ListUploadsResponse {
                    uploads: items,
                    next_page_token: String::new(),
                }));
            }
            Err(error) if scoped => {
                warn!(%error, "scoped upload list pool checkout failed");
                Err(Status::unavailable("upload list database unavailable"))
            }
            Err(error) => Err(Status::internal(error.to_string())),
        }
    }

    async fn resume_upload(
        &self,
        request: Request<pb::ResumeUploadRequest>,
    ) -> Result<Response<pb::ResumeUploadResponse>, Status> {
        // Ownership call site 5 of 5.
        let presented =
            extract_presented_credentials(&request, self.state.require_robot_certificate_identity)?;
        let identity = resolve_request_identity(&self.state, presented).await?;
        let payload = request.into_inner();
        if payload.resume_version != 1 {
            return Err(Status::invalid_argument("unsupported resume_version"));
        }
        resumable::resume(&self.state, payload.upload_id, &identity).await
    }

    async fn health_check(
        &self,
        _request: Request<pb::HealthCheckRequest>,
    ) -> Result<Response<pb::HealthCheckResponse>, Status> {
        // Advertise the deployed per-robot concurrency limit so clients
        // (see transport.py's `server_per_robot_stream_limit`) can size their
        // own concurrency instead of guessing a hardcoded default.
        let checks = health_checks(self.state.per_robot_limit);
        let resp = pb::HealthCheckResponse {
            status: "healthy".into(),
            checks,
            timestamp: None,
        };
        Ok(Response::new(resp))
    }
}

/// The `checks` map advertised by `HealthCheck`. Keyed
/// `per_robot_concurrency` to match the first key the Python client's
/// `server_per_robot_stream_limit` looks for (see transport.py's
/// `PER_ROBOT_LIMIT_CHECK_KEYS`) so it learns the deployed limit instead of
/// falling back to a hardcoded assumption.
fn health_checks(per_robot_limit: usize) -> std::collections::HashMap<String, String> {
    std::collections::HashMap::from([(
        "per_robot_concurrency".to_string(),
        per_robot_limit.to_string(),
    )])
}

#[allow(clippy::result_large_err)] // Tonic fixes RPC errors to its concrete Status type.
fn try_acquire_per_robot(
    state: &AppState,
    robot_id: &str,
    limit: usize,
) -> Result<tokio::sync::OwnedSemaphorePermit, Status> {
    let sem = state
        .per_robot
        .entry(robot_id.to_string())
        .or_insert_with(|| Arc::new(Semaphore::new(limit)))
        .clone();
    sem.try_acquire_owned()
        .map_err(|_| Status::resource_exhausted("per-robot upload capacity exceeded"))
}

fn storage_path(
    root: &std::path::Path,
    robot_id: &str,
    upload_id: &str,
    filename: &str,
) -> PathBuf {
    let binding = PathBuf::from(filename);
    let ext = binding
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_string();
    let now = Utc::now();
    root.join("raw")
        .join("mcap")
        .join(robot_id)
        .join(format!("{:04}", now.year()))
        .join(format!("{:02}", now.month()))
        .join(format!("{:02}", now.day()))
        .join(format!("{}.{}", upload_id, ext))
}

fn path_to_s3_key(p: &std::path::Path) -> String {
    // Convert local path under storage_root to s3 key like raw/mcap/...
    // Here we just strip drive prefixes and use forward slashes
    let s = p.to_string_lossy().replace('\\', "/");
    // Find "/raw/" segment and slice from there; else use full path
    if let Some(idx) = s.find("/raw/") {
        s[idx + 1..].to_string()
    } else {
        s.trim_start_matches('/').to_string()
    }
}

fn manifest_object_key(robot_id: &str, upload_id: &str) -> String {
    format!("manifests/{}/{}.json", robot_id, upload_id)
}

#[allow(clippy::too_many_arguments)] // Fields map one-to-one to the manifest contract.
fn cas_manifest_document(
    upload_id: &str,
    robot_id: &str,
    original_filename: &str,
    size_bytes: i64,
    content_type: &str,
    file_sha256: &str,
    cas_key: &str,
    storage_path: &str,
) -> String {
    serde_json::json!({
        "upload_id": upload_id,
        "robot_id": robot_id,
        "original_filename": original_filename,
        "size_bytes": size_bytes,
        "content_type": content_type,
        "file_sha256": file_sha256,
        "cas_key": cas_key,
        "storage_path": storage_path,
    })
    .to_string()
}

/// Make one unary CAS upload and its per-upload manifest durable.
///
/// Ordering is intentional and closes the cross-system GC hole:
///
/// 1. The caller has already committed an `uploads` reservation containing
///    `storage_path`, so the CAS data key is reachable before this function.
/// 2. Put the verified data bytes.  Replays overwrite the same digest key with
///    the same bytes, confirming object durability instead of trusting a HEAD.
/// 3. Register the upload-owned manifest path in Postgres.
/// 4. Only then put the manifest JSON object, so no manifest object ever exists
///    without a GC root.  The caller marks `uploads.status='completed'` only
///    after this function returns.
///
/// A crash can leave a non-completed upload and, at worst, a registry row whose
/// manifest object has not landed yet.  An exact retry deterministically fills
/// that object; it cannot leave an unregistered object for GC to delete.
#[allow(clippy::too_many_arguments)] // Coordinates one manifest and its CAS object atomically.
async fn ensure_cas_upload_artifacts(
    pg: &Pool,
    s3: &s3::Client,
    bucket: &str,
    cas_key: &str,
    upload_id: &str,
    robot_id: &str,
    original_filename: &str,
    size_bytes: i64,
    file_sha256: &str,
    storage_path: &str,
    data: Vec<u8>,
) -> Result<(), Status> {
    s3.put_object()
        .bucket(bucket)
        .key(cas_key)
        .body(ByteStream::from(data))
        .send()
        .await
        .map_err(|error| {
            warn!(%error, %upload_id, %cas_key, "CAS data object write failed");
            Status::internal("cas_data_object_write_failed")
        })?;

    let manifest_key = manifest_object_key(robot_id, upload_id);
    let manifest_s3_path = format!("s3://{}/{}", bucket, manifest_key);
    let authoritative_content_type = pg_register_manifest(
        pg,
        upload_id,
        file_sha256,
        size_bytes,
        cas_key,
        &manifest_s3_path,
        storage_path,
    )
    .await
    .map_err(|error| {
        error!(%error, %upload_id, "CAS manifest registry write failed");
        Status::internal("manifest_registry_write_failed")
    })?;

    let manifest = cas_manifest_document(
        upload_id,
        robot_id,
        original_filename,
        size_bytes,
        &authoritative_content_type,
        file_sha256,
        cas_key,
        storage_path,
    );
    s3.put_object()
        .bucket(bucket)
        .key(&manifest_key)
        .body(ByteStream::from(manifest.into_bytes()))
        .send()
        .await
        .map_err(|error| {
            warn!(%error, %upload_id, %manifest_key, "CAS manifest object write failed");
            Status::internal("manifest_object_write_failed")
        })?;
    Ok(())
}

// Prove the already-bootstrapped bucket is writable by this runtime identity.
// Bucket creation and policy administration belong exclusively to the release
// bootstrap job; granting them to ingest would defeat datastore least privilege.
// A unique key prevents concurrent readiness probes deleting each other's object.
async fn ensure_bucket_ready(client: &s3::Client, bucket: &str) -> anyhow::Result<()> {
    let probe_key = format!("_health/ingest/{}", Uuid::new_v4());
    client
        .put_object()
        .bucket(bucket)
        .key(&probe_key)
        .body(ByteStream::from_static(b"ok"))
        .send()
        .await?;
    client
        .delete_object()
        .bucket(bucket)
        .key(&probe_key)
        .send()
        .await?;
    Ok(())
}

async fn s3_is_ready(client: Option<&s3::Client>, bucket: Option<&str>) -> bool {
    match (client, bucket) {
        (Some(client), Some(bucket)) => match ensure_bucket_ready(client, bucket).await {
            Ok(_) => true,
            Err(error) => {
                warn!(%error, "S3 readiness check failed");
                false
            }
        },
        _ => {
            // Raw uploads have no local-filesystem fallback. Reporting ready
            // here would admit traffic that every upload RPC must reject with
            // `s3_not_configured`.
            warn!("S3 readiness check failed: S3 client or bucket is not configured");
            false
        }
    }
}

/// Keep the readiness query deliberately side-effect free.  Acquiring a pool
/// connection alone is not enough: Postgres may accept a TCP connection while
/// still failing queries (for example during recovery or after a bad
/// migration), so `/readyz` executes this statement on every probe.
const POSTGRES_READINESS_QUERY: &str = "SELECT 1";

async fn postgres_is_ready(pg: &Pool) -> bool {
    match pg.get().await {
        Ok(client) => match client.query_one(POSTGRES_READINESS_QUERY, &[]).await {
            Ok(_) => true,
            Err(error) => {
                warn!(%error, "postgres readiness query failed");
                false
            }
        },
        Err(error) => {
            warn!(%error, "postgres readiness pool checkout failed");
            false
        }
    }
}

fn readiness_status(s3_ready: bool, postgres_ready: bool) -> StatusCode {
    if s3_ready && postgres_ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

/// Docker executes this through `ingest-server --healthcheck`. Keeping the
/// probe in the application binary means the final image has no curl/wget
/// package merely to request its own loopback readiness endpoint.
const LOCAL_HEALTHCHECK_ADDRESS: &str = "127.0.0.1:8080";
const LOCAL_HEALTHCHECK_REQUEST: &[u8] =
    b"GET /readyz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
// The probe only needs an HTTP status line. Bound the bytes retained so a
// broken loopback listener cannot turn the healthcheck process into an
// unbounded reader, while allowing much more than any valid HTTP status line.
const LOCAL_HEALTHCHECK_STATUS_LINE_LIMIT: usize = 512;

fn healthcheck_response_is_ready(response: &[u8]) -> bool {
    response.starts_with(b"HTTP/1.1 200 ") || response.starts_with(b"HTTP/1.0 200 ")
}

/// Read a complete HTTP status line without assuming one TCP read contains it.
/// TCP is a stream: a successful `/readyz` response may split `HTTP/1.1 200`
/// across arbitrary reads. Stop at the first CRLF, EOF, or the bounded limit;
/// callers treat an incomplete or non-200 line as unhealthy.
fn read_healthcheck_status_line(reader: &mut impl Read) -> std::io::Result<Vec<u8>> {
    let mut status_line = Vec::with_capacity(64);
    let mut chunk = [0_u8; 64];

    while status_line.len() < LOCAL_HEALTHCHECK_STATUS_LINE_LIMIT {
        let remaining = LOCAL_HEALTHCHECK_STATUS_LINE_LIMIT - status_line.len();
        let chunk_limit = remaining.min(chunk.len());
        let received = reader.read(&mut chunk[..chunk_limit])?;
        if received == 0 {
            break;
        }
        status_line.extend_from_slice(&chunk[..received]);
        if let Some(crlf_at) = status_line.windows(2).position(|bytes| bytes == b"\r\n") {
            status_line.truncate(crlf_at + 2);
            break;
        }
    }

    Ok(status_line)
}

fn run_local_healthcheck() -> Result<(), String> {
    let address: SocketAddr = LOCAL_HEALTHCHECK_ADDRESS
        .parse()
        .map_err(|error| format!("invalid built-in healthcheck address: {error}"))?;
    let timeout = Duration::from_secs(8);
    let mut stream = TcpStream::connect_timeout(&address, timeout)
        .map_err(|error| format!("ingest readiness connection failed: {error}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|error| format!("ingest readiness read timeout setup failed: {error}"))?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|error| format!("ingest readiness write timeout setup failed: {error}"))?;
    stream
        .write_all(LOCAL_HEALTHCHECK_REQUEST)
        .map_err(|error| format!("ingest readiness request failed: {error}"))?;

    let response = read_healthcheck_status_line(&mut stream)
        .map_err(|error| format!("ingest readiness response failed: {error}"))?;
    if healthcheck_response_is_ready(&response) {
        Ok(())
    } else {
        Err("ingest readiness endpoint did not return HTTP 200".to_string())
    }
}

// ========== Ingest authentication ==========
//
// Without this gate the gRPC data plane and the axum admin router would accept
// any caller that could open a socket, so any LAN peer could upload or read the
// upload index. Two fronts close that: compose does not publish the data plane
// (see deploy/docker-compose.debug.yml for the opt-in), and every data-plane
// call must present a shared token in an `authorization` header/metadata entry.
//
// The token is read once from `INGEST_AUTH_TOKEN` at startup:
//   - set (non-empty) → UploadService RPCs and `POST /admin/robots` require it;
//   - unset/empty     → auth is OFF and startup logs a loud warning.
// Fail-open-when-unset is deliberate, with the warning above.
// docker-compose.yml always sets the variable, so the shipped stack is
// authenticated by default (see docs/OPERATIONS.md).
//
// The health, reflection, `/healthz`, `/readyz` and `/metrics` endpoints stay
// unauthenticated on purpose: they carry no upload data and are what the
// compose healthcheck / Prometheus scrape use.

/// Header (HTTP) / metadata key (gRPC) carrying the shared ingest token. Both
/// transports use the same name so one `INGEST_AUTH_TOKEN` covers both.
const AUTH_HEADER: &str = "authorization";
/// Optional RFC 6750 scheme name; `authorization: Bearer <token>` and a bare
/// `authorization: <token>` are both accepted.
const BEARER_SCHEME: &str = "Bearer";
/// Outcome of checking one request's credential. `Allowed` also covers the
/// "auth disabled" case so callers have a single accept path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AuthOutcome {
    Allowed,
    /// No `authorization` header/metadata at all (or an empty one).
    MissingCredential,
    /// A credential was presented but did not match.
    InvalidCredential,
}

/// Pure auth decision, so the gRPC interceptor and the admin middleware share
/// one implementation and `mod tests` can assert it without a live server.
///
/// `expected == None` (or empty) means auth is disabled → everything is
/// allowed. Otherwise the presented value must equal the expected token, with
/// an optional case-insensitive `Bearer ` prefix stripped first.
fn evaluate_auth(expected: Option<&str>, presented: Option<&str>) -> AuthOutcome {
    let expected = match expected {
        Some(e) if !e.trim().is_empty() => e.trim(),
        _ => return AuthOutcome::Allowed,
    };
    let presented = match presented {
        Some(p) => p.trim(),
        None => return AuthOutcome::MissingCredential,
    };
    // Split on the first whitespace so a bare `Bearer` (no credential) is
    // recognised as missing rather than compared as if it were the token, and
    // so a multi-byte value can never split a char boundary.
    let (scheme, rest) = presented
        .split_once(char::is_whitespace)
        .unwrap_or((presented, ""));
    let token = if scheme.eq_ignore_ascii_case(BEARER_SCHEME) {
        rest.trim()
    } else {
        presented
    };
    if token.is_empty() {
        return AuthOutcome::MissingCredential;
    }
    if constant_time_eq(expected.as_bytes(), token.as_bytes()) {
        AuthOutcome::Allowed
    } else {
        AuthOutcome::InvalidCredential
    }
}

/// Compare two secrets without an early exit on the first differing byte, so a
/// caller cannot recover the token one byte at a time by timing the reply. The
/// length check is not constant time (it leaks only the token's length, which
/// is not secret).
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// Map a rejected outcome to the gRPC status returned to the client. Kept
/// separate from `evaluate_auth` so the pure decision stays transport-agnostic.
fn auth_status(outcome: AuthOutcome) -> Option<Status> {
    match outcome {
        AuthOutcome::Allowed => None,
        AuthOutcome::MissingCredential => {
            counter!("ingest_auth_rejected_total", "reason" => "missing").increment(1);
            Some(Status::unauthenticated(
                "missing ingest auth token (send `authorization: Bearer <INGEST_AUTH_TOKEN>`)",
            ))
        }
        AuthOutcome::InvalidCredential => {
            counter!("ingest_auth_rejected_total", "reason" => "invalid").increment(1);
            Some(Status::unauthenticated("invalid ingest auth token"))
        }
    }
}

/// Resolve the credential-cache TTL / mid-stream re-check interval.
///
/// Revocation must take effect within 30 s, so 30 s is a **ceiling**,
/// not a default that an environment variable may raise. An operator who wants
/// a tighter interval can have one; an operator who fat-fingers `3000` gets 30.
fn resolve_auth_recheck_interval(configured: Option<&str>) -> Duration {
    let ceiling_seconds = robot_auth::CREDENTIAL_CACHE_TTL.as_secs();
    let seconds = configured
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(ceiling_seconds)
        .min(ceiling_seconds);
    Duration::from_secs(seconds)
}

// `GET /ca.crt` is NOT served here, deliberately. The route belongs to the
// query service on 8003: ingest's
// HTTP surface is 8080, which stays unpublished, and its published
// port is 50051 gRPC — so an ingest-hosted `/ca.crt` could not be reached by the
// robot that is its only caller. Ingest still *reads* the same public CA file,
// but only to mint the CA pin carried inside an enrollment code.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--healthcheck") {
        return run_local_healthcheck().map_err(anyhow::Error::msg);
    }
    tracing_subscriber::fmt().with_env_filter("info").init();
    let storage_root =
        PathBuf::from(std::env::var("STORAGE_ROOT").unwrap_or_else(|_| "/app/uploads".to_string()));
    let grpc_addr: SocketAddr = "0.0.0.0:50051".parse().unwrap();
    let admin_addr: SocketAddr = "0.0.0.0:8080".parse().unwrap();

    // ===== metrics =====
    //
    // Order matters here. `describe_*` are recorder calls exactly like `counter!`
    // is, so any that run before a recorder is installed are handed to the global
    // no-op sink and lost, and a scrape would carry no HELP/TYPE lines. Install
    // first, describe second.
    //
    // The binary must also link exactly one `metrics` major: two majors mean two
    // global recorders and an empty body. See the comment on `metrics` in
    // Cargo.toml — both rules have to hold for this endpoint to answer.
    let prom_handle = PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder");
    describe_counter!("uploads_completed", "Count of completed uploads by method");
    describe_counter!("upload_bytes_total", "Bytes received by method");
    describe_histogram!(
        "ingest_write_seconds",
        "Time spent writing raw files (fs or s3)"
    );
    describe_histogram!("ingest_part_upload_seconds", "Duration of one S3 upload_part call; with bounded pipelining these overlap each other and the gRPC receive");
    describe_gauge!(
        "ingest_inflight_uploads",
        "Number of inflight uploads, sampled from the global concurrency semaphore at scrape time"
    );
    describe_counter!("completed_row_write_failures_total", "Uploads whose S3 object was written but whose completed 'uploads' row could not be persisted; the RPC returns Status::internal so the client retries");
    describe_counter!(
        "ingest_auth_rejected_total",
        "Requests rejected by the shared-token check, by reason (missing/invalid)"
    );
    describe_counter!(
        "ingest_transport_admission_rejected_total",
        "Payload upload RPCs rejected before protobuf decoding because process-wide transport capacity was full"
    );
    describe_counter!("ingest_robot_rejected_total", "Uploads refused because robot_id is missing/malformed or not in the `robots` fleet registry, by reason");
    describe_counter!("ingest_robot_identity_rejected_total", "Release uploads rejected because the authenticated client-certificate URI SAN is missing, malformed or does not match robot_id");
    describe_counter!("ingest_upload_reservation_rejected_total", "Upload reservations rejected before S3 because an id is busy or immutable metadata conflicts");
    describe_counter!("ingest_upload_idempotent_replays_total", "Completed uploads returned directly from their immutable reservation without repeating S3 I/O");
    describe_counter!("ingest_enrollment_rejected_total", "RobotEnrollment.Enroll attempts refused, by server-side reason (the client always sees one constant failure)");
    describe_counter!(
        "ingest_enrollment_completed_total",
        "Enrollment codes successfully exchanged for a per-robot credential"
    );
    describe_counter!(
        "ingest_enrollment_codes_minted_total",
        "One-use enrollment codes minted through /admin/v2"
    );
    describe_counter!(
        "ingest_provisioning_auth_rejected_total",
        "Requests to /admin/v2/robots* refused by the provisioning-token check, by reason"
    );
    describe_counter!(
        "ingest_stream_aborted_total",
        "In-flight streaming uploads terminated by the mid-stream credential/robot-state re-check"
    );

    // Shared-token auth. Empty/unset is still fail-open in `dev`, but the
    // profile rules require both independent operator/provisioning secrets in the two
    // deployed profiles.
    let auth_token = std::env::var("INGEST_AUTH_TOKEN")
        .ok()
        .filter(|t| !t.trim().is_empty());
    let provisioning_token = std::env::var("INGEST_PROVISIONING_TOKEN")
        .ok()
        .filter(|t| !t.trim().is_empty());
    let security_profile =
        resolve_ingest_security_profile(std::env::var("INGEST_SECURITY_PROFILE").ok().as_deref())
            .map_err(anyhow::Error::msg)?;
    let shared_token_policy = resolve_shared_token_policy(
        security_profile,
        std::env::var("INGEST_SHARED_TOKEN_POLICY").ok().as_deref(),
    )
    .map_err(anyhow::Error::msg)?;
    let tls_enabled =
        std::env::var("TLS_ENABLED").unwrap_or_else(|_| "false".to_string()) == "true";
    let tls_mtls = security_profile
        .resolve_tls_mtls(std::env::var("TLS_MTLS").ok().as_deref())
        .map_err(anyhow::Error::msg)?;
    if let Some(message) = profile_configuration_error(
        security_profile,
        tls_enabled,
        tls_mtls,
        shared_token_policy,
        auth_token.as_deref(),
        provisioning_token.as_deref(),
    ) {
        anyhow::bail!(message);
    }
    // A deployment switch, not a per-robot setting, so it cannot drift
    // across a fleet. An unknown value is a startup error for the same reason
    // an unknown profile is: silently defaulting is how a change-controlled
    // site ends up with unattended credential rotation it forbade.
    let self_rotation_enabled = match std::env::var("INGEST_ROBOT_SELF_ROTATION")
        .ok()
        .as_deref()
        .map(str::trim)
    {
        None | Some("") | Some("enabled") => true,
        Some("disabled") => false,
        Some(other) => anyhow::bail!(
            "INGEST_ROBOT_SELF_ROTATION must be one of enabled, disabled (got {other:?})"
        ),
    };
    // Returned to the robot as `ingest_endpoint`, and it must match the
    // server certificate the robot will verify.
    let ingest_endpoint = public_ingest_endpoint(
        std::env::var("INGEST_PUBLIC_HOST").ok().as_deref(),
        std::env::var("INGEST_PUBLIC_PORT").ok().as_deref(),
        grpc_addr.port(),
    )
    .map_err(anyhow::Error::msg)?;
    match auth_token.as_deref() {
        Some(t) if t.trim() == DEV_DEFAULT_TOKEN => warn!(
            "auth enabled with the committed dev default token — set INGEST_AUTH_TOKEN to a real secret before exposing ingest beyond localhost"
        ),
        Some(t) => info!("auth enabled (shared token, {} chars)", t.trim().len()),
        None => warn!(
            "auth DISABLED (INGEST_AUTH_TOKEN unset): the gRPC data plane and POST /admin/robots accept unauthenticated callers"
        ),
    }
    if security_profile.requires_robot_certificate_identity() {
        info!(
            "mtls robot identity binding enabled: client leaf certificates require URI SAN {}<percent-encoded-robot-id>",
            ROBOT_SPIFFE_URI_PREFIX
        );
    }

    // Local SQLite index removed. These settings directly size semaphores and
    // the Postgres pool, so zero, malformed, and excessive values stop startup
    // instead of producing a healthy-looking server that hangs or overcommits.
    let per_robot_limit = read_concurrency_limit(
        "PER_ROBOT_CONCURRENCY",
        DEFAULT_PER_ROBOT_CONCURRENCY,
        MAX_PER_ROBOT_CONCURRENCY,
    )?;
    let global_limit = read_concurrency_limit(
        "GLOBAL_CONCURRENCY",
        DEFAULT_GLOBAL_CONCURRENCY,
        MAX_GLOBAL_CONCURRENCY,
    )?;
    // Per-stream S3 part-upload pipelining depth. The combined setting is
    // checked below because every active stream can retain its in-flight parts
    // plus the chunk currently being validated.
    let max_inflight_parts =
        resolve_max_inflight_parts(std::env::var("MAX_INFLIGHT_PARTS").ok().as_deref());
    let upload_payload_ceiling = validate_upload_payload_budget(global_limit, max_inflight_parts)
        .map_err(anyhow::Error::msg)?;
    let first_message_timeout = read_timeout_seconds(
        "INGEST_FIRST_MESSAGE_TIMEOUT_SECONDS",
        DEFAULT_FIRST_MESSAGE_TIMEOUT_SECONDS,
        MAX_STREAM_TIMEOUT_SECONDS,
    )?;
    let stream_idle_timeout = read_timeout_seconds(
        "INGEST_STREAM_IDLE_TIMEOUT_SECONDS",
        DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        MAX_STREAM_TIMEOUT_SECONDS,
    )?;
    info!(
        "upload limits: stream_chunk_ceiling_bytes={} upload_payload_ceiling_bytes={} max_inflight_parts={} global_concurrency={} per_robot={} first_message_timeout_seconds={} stream_idle_timeout_seconds={}",
        STREAM_CHUNK_CEILING_BYTES,
        upload_payload_ceiling,
        max_inflight_parts,
        global_limit,
        per_robot_limit,
        first_message_timeout.as_secs_f64(),
        stream_idle_timeout.as_secs_f64(),
    );

    // Optional S3
    let s3_bucket = std::env::var("S3_BUCKET").ok();
    let s3_endpoint = std::env::var("S3_ENDPOINT").ok();
    let s3_region = std::env::var("S3_REGION")
        .ok()
        .unwrap_or_else(|| "us-east-1".to_string());
    let s3_client: Option<s3::Client> = if s3_bucket.is_some() {
        let mut loader = aws_config::defaults(aws_config::BehaviorVersion::latest());
        if let Some(ep) = &s3_endpoint {
            loader = loader.endpoint_url(ep);
        }
        // Ensure region is set explicitly (the object store takes any region; the contract uses us-east-1)
        loader = loader.region(AwsRegion::new(s3_region.clone()));
        let conf = loader.load().await;
        let mut cfg = s3::config::Builder::from(&conf);
        // Path style on every client (development/objectstore/CONTRACT.md section 3)
        cfg = cfg.force_path_style(true);
        let client = s3::Client::from_conf(cfg.build());
        Some(client)
    } else {
        None
    };

    // Optional Postgres pool
    let db_url =
        std::env::var("DATABASE_URL").expect("DATABASE_URL is required for Postgres-only ingest");
    let pg: Pool = match db_url.parse::<tokio_postgres::Config>() {
        Ok(cfg) => {
            let mgr = Manager::new(cfg, NoTls);
            // Each live upload intentionally holds one connection for its
            // cross-process advisory lock.  Keep additional capacity for
            // progress, completion, readiness and admin queries.
            Pool::builder(mgr)
                .max_size(global_limit.saturating_add(8).max(16))
                .build()
                .unwrap()
        }
        Err(e) => panic!("postgres_config_error: {}", e),
    };
    info!("postgres pool configured");
    let auth_recheck_interval = resolve_auth_recheck_interval(
        std::env::var("INGEST_CREDENTIAL_RECHECK_SECONDS")
            .ok()
            .as_deref(),
    );
    let credential_cache = CredentialCache::new(auth_recheck_interval);
    let robot_registry = RobotRegistryCache::new(auth_recheck_interval);
    let state = AppState {
        storage_root,
        global_sem: Arc::new(Semaphore::new(global_limit)),
        per_robot: Arc::new(DashMap::new()),
        per_robot_limit,
        max_inflight_parts,
        first_message_timeout,
        stream_idle_timeout,
        s3_client: s3_client.clone(),
        s3_bucket: s3_bucket.clone(),
        include_robot_labels: std::env::var("METRICS_INCLUDE_ROBOT_ID")
            .unwrap_or_else(|_| "false".to_string())
            == "true",
        pg,
        enable_file_cas: std::env::var("ENABLE_FILE_CAS").unwrap_or_else(|_| "false".to_string())
            == "true",
        robot_registry: robot_registry.clone(),
        credential_cache: credential_cache.clone(),
        require_robot_certificate_identity: security_profile.requires_robot_certificate_identity(),
        shared_token_policy,
        auth_token: auth_token.clone(),
        auth_recheck_interval,
    };
    // Ensure bucket exists and is writable before serving traffic (best-effort)
    if let (Some(client), Some(bucket)) = (&s3_client, &s3_bucket) {
        let _ = ensure_bucket_ready(client, bucket).await;
    }

    let ingest = IngestSvc {
        state: state.clone(),
    };
    let admin_pg_pool_robots_add = state.pg.clone();
    let admin_registry_cache = state.robot_registry.clone();
    // Provisioning a robot. `name` defaults to the id; `metadata` is a
    // JSON object string for fleet attributes (site, model, ...).
    #[derive(serde::Deserialize)]
    struct RobotRegisterQuery {
        robot_id: String,
        name: Option<String>,
        metadata: Option<String>,
    }
    let s3_for_ready = state.s3_client.clone();
    let bucket_for_ready = state.s3_bucket.clone();
    let pg_for_ready = state.pg.clone();
    let metrics_global_sem = state.global_sem.clone();
    // Unauthenticated probe surface: liveness, readiness, Prometheus scrape.
    // Carries no upload data, and is what the compose healthcheck and the
    // `obs`-profile Prometheus job poll.
    let admin_probes = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route(
            "/readyz",
            get(move || {
                let s3_opt = s3_for_ready.clone();
                let b_opt = bucket_for_ready.clone();
                let pg = pg_for_ready.clone();
                async move {
                    let s3_ready = s3_is_ready(s3_opt.as_ref(), b_opt.as_deref()).await;
                    let postgres_ready = postgres_is_ready(&pg).await;
                    readiness_status(s3_ready, postgres_ready)
                }
            }),
        )
        .route(
            "/metrics",
            get(move || {
                let handle = prom_handle.clone();
                let sem = metrics_global_sem.clone();
                async move {
                    // `ingest_inflight_uploads` is sampled here rather than tracked
                    // with an increment/decrement pair around each RPC: the permits
                    // taken out of the global semaphore ARE the number of in-flight
                    // uploads, and reading them at scrape time cannot leak a stuck
                    // +1 when an upload returns early (there are eleven such exits).
                    gauge!("ingest_inflight_uploads")
                        .set(global_limit.saturating_sub(sem.available_permits()) as f64);
                    handle.render()
                }
            }),
        );
    // Development robot registration. `POST /admin/robots` creates an
    // `('enrolled','shared')` row for the shared-token data plane that dev
    // stacks, seed scripts and E2E tests use. It sits behind the same shared
    // token as the gRPC data plane. Robots with their own credential are
    // created through `/admin/v2/robots` instead. Upload, manifest and robot
    // listings are served by the query service.
    let admin_protected = Router::new()
        .route("/admin/robots", post(move |Query(q): Query<RobotRegisterQuery>| {
            let pg = admin_pg_pool_robots_add.clone();
            let cache = admin_registry_cache.clone();
            let policy = shared_token_policy;
            async move {
                // This route creates `('enrolled','shared')` rows
                // only, so `deny` refuses it. `token` and `mtls` robots
                // are created through `/admin/v2/robots`.
                if policy == SharedTokenPolicy::Deny {
                    return (
                        StatusCode::CONFLICT,
                        Json(serde_json::json!({
                            "error": "shared_credentials_denied",
                            "detail": "INGEST_SHARED_TOKEN_POLICY=deny; use POST /admin/v2/robots"
                        })),
                    );
                }
                // Same shape rules the upload path enforces, so a robot can
                // never be registered under an id an upload could not use.
                let id = match validate_robot_id(&q.robot_id) {
                    Ok(id) => id.to_string(),
                    Err(e) => return (
                        StatusCode::BAD_REQUEST,
                        Json(serde_json::json!({"error": e.label(), "detail": e.message(&q.robot_id)})),
                    ),
                };
                let name = q.name.filter(|n| !n.trim().is_empty()).unwrap_or_else(|| id.clone());
                let metadata: Option<serde_json::Value> = match q.metadata.as_deref() {
                    None | Some("") => None,
                    Some(raw) => match serde_json::from_str(raw) {
                        Ok(v) => Some(v),
                        Err(e) => return (
                            StatusCode::BAD_REQUEST,
                            Json(serde_json::json!({"error": "invalid_metadata_json", "detail": e.to_string()})),
                        ),
                    },
                };
                match pg.get().await {
                    Ok(client) => {
                        // Idempotent: re-registering an existing robot is a
                        // no-op rather than an error, so a seed/provisioning
                        // script can run repeatedly. It deliberately does NOT
                        // overwrite `name`/`metadata` — the registry row is
                        // operator-owned once it exists.
                        match client.execute(
                            "INSERT INTO robots (id, name, metadata, state, auth_mode) VALUES ($1, $2, $3, 'enrolled', 'shared') ON CONFLICT (id) DO NOTHING",
                            &[&id, &name, &metadata],
                        ).await {
                            Ok(n) => {
                                cache.invalidate(&id);
                                info!("robot_registered: id={} created={}", id, n > 0);
                                (StatusCode::OK, Json(serde_json::json!({"robot_id": id, "name": name, "created": n > 0})))
                            }
                            Err(e) => (
                                StatusCode::SERVICE_UNAVAILABLE,
                                Json(serde_json::json!({"error": "robot_registry_unavailable", "detail": e.to_string()})),
                            ),
                        }
                    }
                    Err(e) => (
                        StatusCode::SERVICE_UNAVAILABLE,
                        Json(serde_json::json!({"error": "pg_pool_error", "detail": e.to_string()})),
                    ),
                }
            }
        }));
    // ===== The CA pin =======================================================
    //
    // `TLS_CA_CERT` is the one CA that issued the ingest server certificate.
    // Ingest pins it into each enrollment code (and, under `mtls`, also trusts
    // it for client certificates below). It does **not** serve the file —
    // `GET /ca.crt` is the query service's route, which reads the same host file
    // through `INGEST_CA_CERT_FILE` — so ingest only has to agree with it about
    // the digest. One variable here means the pin and the TLS anchor cannot drift.
    let ca_cert_path =
        std::env::var("TLS_CA_CERT").unwrap_or_else(|_| "/app/certs/ca.crt".to_string());

    // Minted once at startup from the same bytes `/robot` will download and
    // hash. `None` disables code minting rather than producing a code whose pin
    // cannot match anything.
    let (ca_pin, ca_spki_sha256_hex) = match std::fs::read(&ca_cert_path) {
        Ok(bytes) => match robot_auth::ca_spki_sha256_from_pem(&bytes) {
            Ok(digest) => {
                let pin = crockford::ca_pin_from_spki_digest(&digest);
                match pin.as_deref() {
                    Some(pin) => info!(
                        "enrollment CA pin {} (spki sha256 {})",
                        pin,
                        hex::encode(digest)
                    ),
                    None => warn!("CA SPKI digest could not be encoded as a pin"),
                }
                (pin, Some(hex::encode(digest)))
            }
            Err(detail) => {
                warn!(%detail, path = %ca_cert_path, "CA certificate unusable for pinning; enrollment code minting disabled");
                (None, None)
            }
        },
        Err(error) => {
            warn!(%error, path = %ca_cert_path, "CA certificate unreadable; enrollment code minting disabled");
            (None, None)
        }
    };

    // ===== /admin/v2/robots* — the provisioning plane =======================
    let provisioning_router = admin_v2::router(
        admin_v2::ProvisioningState {
            pool: state.pg.clone(),
            credential_cache: credential_cache.clone(),
            registry_cache: robot_registry.clone(),
            ca_pin,
            ca_spki_sha256_hex,
            ingest_endpoint: ingest_endpoint.clone(),
        },
        provisioning_token.clone(),
    );

    // `route_layer` (not `layer`): the check only runs for requests that match
    // `/admin/robots`, so an unknown path still 404s instead of being answered
    // with 401.
    let admin_auth_token = auth_token.clone();
    let admin_app = admin_probes
        .merge(provisioning_router)
        .merge(admin_protected.route_layer(axum::middleware::from_fn(
            move |req: axum::extract::Request, next: axum::middleware::Next| {
                let expected = admin_auth_token.clone();
                async move {
                    let outcome = {
                        let presented =
                            req.headers().get(AUTH_HEADER).and_then(|v| v.to_str().ok());
                        evaluate_auth(expected.as_deref(), presented)
                    };
                    match outcome {
                        AuthOutcome::Allowed => next.run(req).await,
                        AuthOutcome::MissingCredential => {
                            counter!("ingest_auth_rejected_total", "reason" => "missing")
                                .increment(1);
                            (StatusCode::UNAUTHORIZED, "missing ingest auth token\n")
                                .into_response()
                        }
                        AuthOutcome::InvalidCredential => {
                            counter!("ingest_auth_rejected_total", "reason" => "invalid")
                                .increment(1);
                            (StatusCode::UNAUTHORIZED, "invalid ingest auth token\n")
                                .into_response()
                        }
                    }
                }
            },
        )));

    // Keep both serving futures in the main task. If either listener fails or
    // exits, the process fails and orchestration can restart the whole service
    // instead of leaving a half-healthy data or admin plane behind.
    let admin_listener = tokio::net::TcpListener::bind(admin_addr).await?;
    let admin = axum::serve(admin_listener, admin_app);

    let server_cert_path =
        std::env::var("TLS_SERVER_CERT").unwrap_or_else(|_| "/app/certs/ingest.crt".to_string());
    let server_key_path =
        std::env::var("TLS_SERVER_KEY").unwrap_or_else(|_| "/app/certs/ingest.key".to_string());

    // gRPC server builder with compression and http2 tuning
    let mut builder = Server::builder()
        .accept_http1(true) // allow proxy compatibility
        .tcp_keepalive(Some(Duration::from_secs(30)))
        .http2_keepalive_interval(Some(Duration::from_secs(30)))
        .http2_keepalive_timeout(Some(Duration::from_secs(10)))
        .max_frame_size(Some(1 << 20));
    if tls_enabled {
        let cert = std::fs::read(&server_cert_path)?;
        let key = std::fs::read(&server_key_path)?;
        let identity = Identity::from_pem(cert, key);
        let mut cfg = ServerTlsConfig::new().identity(identity);
        if tls_mtls {
            let ca = std::fs::read(&ca_cert_path)?;
            cfg = cfg.client_ca_root(Certificate::from_pem(ca));
        }
        builder = builder.tls_config(cfg)?;
    }

    // Health service
    let (mut health_reporter, health_service) = health_reporter();
    health_reporter
        .set_serving::<UploadServiceServer<IngestSvc>>()
        .await;
    health_reporter
        .set_serving::<pb::robot_enrollment_server::RobotEnrollmentServer<enrollment::EnrollmentService>>()
        .await;
    // Reflection service with file descriptors (built in build.rs)
    let fd_set = include_bytes!(concat!(env!("OUT_DIR"), "/upload_descriptor.bin"));
    let reflection = ReflectionBuilder::configure()
        .register_encoded_file_descriptor_set(fd_set)
        .build()?;

    // The upload service — and only it — is wrapped in the shared-token
    // interceptor. Health and reflection stay open so a probe or
    // `grpcurl` still works without the token.
    // Keep the service-wide 50 MiB cap for the unary UploadSmall contract.
    // Streaming Upload separately pins each chunk to its 5-8 MiB part
    // boundary before moving it into the bounded S3 task set.
    let upload_service = UploadServiceServer::new(ingest)
        .accept_compressed(tonic::codec::CompressionEncoding::Gzip)
        .accept_compressed(tonic::codec::CompressionEncoding::Zstd)
        .send_compressed(tonic::codec::CompressionEncoding::Zstd)
        .max_decoding_message_size(GRPC_MESSAGE_CEILING_BYTES)
        .max_encoding_message_size(GRPC_MESSAGE_CEILING_BYTES);
    let grpc_auth_token = auth_token.clone();
    #[allow(clippy::result_large_err)] // Interceptor errors are required to be tonic::Status.
    let upload_service = InterceptedService::new(upload_service, move |req: Request<()>| {
        // Credential checks split across two layers, on purpose. The
        // interceptor is synchronous, so it cannot do the `robot_credentials`
        // lookup; what it *can* do is keep garbage off the handlers exactly as
        // before. A value that is neither the shared token nor a well-formed
        // `ignr1.…` is refused here; a well-formed one is deferred to the
        // handler, which refuses an unknown credential with this same message,
        // so deferring distinguishes nothing an attacker can observe.
        let presented = req
            .metadata()
            .get(AUTH_HEADER)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        match interceptor_decision(grpc_auth_token.as_deref(), presented.as_deref()) {
            InterceptorDecision::Pass | InterceptorDecision::DeferToHandler => Ok(req),
            InterceptorDecision::Reject(outcome) => Err(auth_status(outcome)
                .unwrap_or_else(|| Status::unauthenticated("invalid ingest auth token"))),
        }
    });
    // This wrapper is outside both the interceptor and tonic's generated
    // decoder. Its semaphore is independent from AppState::global_sem, so the
    // retained in-handler gate cannot double-acquire or deadlock it.
    let upload_service = UploadTransportAdmission::new(upload_service, global_limit);

    // A *separate* service from `UploadService`, deliberately outside
    // the shared-token interceptor above — the enrollment code is the
    // credential, and a robot that has one by definition has no bearer yet.
    let enrollment_service =
        pb::robot_enrollment_server::RobotEnrollmentServer::new(enrollment::EnrollmentService {
            pool: state.pg.clone(),
            credential_cache: credential_cache.clone(),
            registry_cache: robot_registry.clone(),
            rate_limiter: Arc::new(enrollment::EnrollmentRateLimiter::with_contract_defaults(
                std::time::Instant::now(),
            )),
            ingest_endpoint: ingest_endpoint.clone(),
            recommended: enrollment::RecommendedUploadSettings {
                chunk_size_bytes: STREAM_CHUNK_CEILING_BYTES as i64,
                parallel_streams: per_robot_limit as i32,
                min_stable_age_seconds: 30.0,
            },
            self_rotation_enabled,
        });

    let grpc = builder
        .add_service(health_service)
        .add_service(reflection)
        .add_service(upload_service)
        .add_service(enrollment_service)
        .serve(grpc_addr);

    info!("admin listening on {}", admin_addr);
    if let (Some(b), Some(ep)) = (s3_bucket.as_ref(), s3_endpoint.as_ref()) {
        info!(
            "s3 configured: bucket={}, endpoint={}, region={}",
            b, ep, s3_region
        );
    } else if let Some(b) = s3_bucket.as_ref() {
        info!("s3 configured: bucket={}, region={}", b, s3_region);
    } else {
        warn!("s3 not configured; raw uploads will fail");
    }
    info!("grpc listening on {}", grpc_addr);
    tokio::select! {
        _ = resumable::sweep_loop(state.clone()) => {
            anyhow::bail!("durable upload expiry worker exited unexpectedly")
        }
        result = admin => {
            result?;
            anyhow::bail!("admin server exited unexpectedly")
        }
        result = grpc => {
            result?;
            anyhow::bail!("gRPC server exited unexpectedly")
        }
    }
}

fn now_ts() -> Option<Timestamp> {
    let now = Utc::now();
    Some(Timestamp {
        seconds: now.timestamp(),
        nanos: now.timestamp_subsec_nanos() as i32,
    })
}

// ========== Postgres helpers (optional dual-write) ==========

/// Capture-metadata intake: the five columns
/// (`operator`/`site`/`permission_ref`/`session_tags`/`task_label`) are
/// optional proto3 strings, so an unset one arrives as `""` (the wire
/// default), never absent. This is what turns that wire-level `""` into a
/// SQL NULL rather than persisting an empty string an operator never typed
/// -- the same "empty string is not a value" reasoning the rest of this file
/// already applies to e.g. `expected_sha256`.
fn empty_to_null(s: &str) -> Option<&str> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// The completed-row upsert, with its immutability guard.
///
/// `WHERE uploads.status <> 'completed'` on the `DO UPDATE` makes a `completed`
/// row terminal: once written, its `sha256_hash`/`storage_path`/`file_size_bytes`
/// (and `robot_id`, which the SET list never touched) can no longer be rewritten
/// by a later caller. Without it any repeat call — a reused `client_upload_id`,
/// a stale retry, a bug — could silently restate the system of record for an
/// already-verified object. `status` is `NOT NULL` (001_baseline.sql) so the
/// comparison is never NULL and never skips a legitimate first completion.
///
/// Kept as a named constant so the guard is assertable from `mod tests` without
/// a live Postgres (this crate has no live-DB test harness — see
/// tests/e2e/test_pipeline_e2e.py for the behavioural coverage).
///
/// Capture-metadata intake: `operator`/`site`/`permission_ref`/
/// `session_tags`/`task_label` ($8-$12) are in BOTH the INSERT list and the
/// DO UPDATE SET list. The reservation insert is their first writer for both RPCs;
/// restating them here records the completing attempt and is safe under the
/// terminal-state plus immutable-identity guard: the SET clause never fires
/// against an already-completed row or a conflicting reservation.
const COMPLETED_UPLOAD_UPSERT_SQL: &str = "INSERT INTO uploads (client_upload_id, robot_id, original_filename, storage_path, file_size_bytes, sha256_hash, expected_sha256, content_type, status, upload_started_at, upload_completed_at, bytes_received, integrity_verified, integrity_verification_basis, operator, site, permission_ref, session_tags, task_label) VALUES ($1, $2, $3, $4, $5, $6, $6, $7, 'completed', NOW(), NOW(), $5, TRUE, 'client_sha256', $8, $9, $10, $11, $12) ON CONFLICT (client_upload_id) DO UPDATE SET status='completed', upload_completed_at=NOW(), sha256_hash=$6, content_type=$7, storage_path=$4, bytes_received=$5, integrity_verified=TRUE, integrity_verification_basis='client_sha256', operator=$8, site=$9, permission_ref=$10, session_tags=$11, task_label=$12 WHERE uploads.status <> 'completed' AND uploads.robot_id=$2 AND uploads.original_filename=$3 AND uploads.file_size_bytes=$5 AND uploads.expected_sha256=$6";

/// Insert/upsert the terminal `completed` row for an upload.
///
/// This `uploads` row is the ONLY signal the processor uses to discover
/// work (`WHERE status='completed' AND p.upload_id IS NULL`). So neither a
/// pool-get failure nor the execute failure may be swallowed here: both propagate as `Err` so the caller
/// can fail the RPC (making the client retry) instead of reporting a completion
/// that was never recorded, which would strand the S3 object invisibly forever.
///
/// The upsert is guarded (see `COMPLETED_UPLOAD_UPSERT_SQL`), so a repeat
/// call against an already-`completed` row writes nothing and reports zero rows.
/// That is only *truthful* to answer `completed` for when this attempt carried
/// the same bytes, so the zero-row case is reconciled against the stored digest:
///
///   - same sha256 -> Ok(()). This is the legitimate retry path for a client
///     whose earlier reply was lost. Returning an error would fail an RPC whose
///     object is demonstrably stored and recorded.
///   - different sha256 -> Err. The pre-built response would otherwise report
///     `completed` with a hash/path the system of record does not hold, so the
///     caller must fail the RPC (`finalize_completion` -> `Status::internal`)
///     rather than certify a completion for content nobody can find.
#[allow(clippy::too_many_arguments)] // Parameters intentionally mirror the completed-row SQL.
async fn pg_insert_completed_upload(
    pg: &Pool,
    upload_id: &str,
    robot_id: &str,
    filename: &str,
    bytes: i64,
    storage_path: &str,
    final_hash: &str,
    content_type: &str,
    operator: &str,
    site: &str,
    permission_ref: &str,
    session_tags: &str,
    task_label: &str,
) -> anyhow::Result<()> {
    let client = pg.get().await?;
    let operator = empty_to_null(operator);
    let site = empty_to_null(site);
    let permission_ref = empty_to_null(permission_ref);
    let session_tags = empty_to_null(session_tags);
    let task_label = empty_to_null(task_label);
    let rows_written = client
        .execute(
            COMPLETED_UPLOAD_UPSERT_SQL,
            &[
                &upload_id,
                &robot_id,
                &filename,
                &storage_path,
                &bytes,
                &final_hash,
                &content_type,
                &operator,
                &site,
                &permission_ref,
                &session_tags,
                &task_label,
            ],
        )
        .await?;
    if rows_written > 0 {
        return Ok(());
    }
    // Zero rows means either the terminal-state guard or the immutable-identity
    // guard fired.  Only an exact match of the whole reserved tuple and stored
    // path is an idempotent success.
    let stored = client
        .query_opt(
            "SELECT robot_id, original_filename, file_size_bytes, expected_sha256, sha256_hash, storage_path FROM uploads WHERE client_upload_id=$1 AND status='completed'",
            &[&upload_id],
        )
        .await?;
    if let Some(row) = stored {
        let stored_robot: String = row.get(0);
        let stored_filename: String = row.get(1);
        let stored_size: i64 = row.get(2);
        let stored_expected: Option<String> = row.get(3);
        let stored_hash: Option<String> = row.get(4);
        let stored_path: String = row.get(5);
        if stored_robot == robot_id
            && stored_filename == filename
            && stored_size == bytes
            && stored_expected
                .as_deref()
                .is_some_and(|digest| digest.eq_ignore_ascii_case(final_hash))
            && stored_hash
                .as_deref()
                .is_some_and(|digest| digest.eq_ignore_ascii_case(final_hash))
            && stored_path == storage_path
        {
            info!("completed_row_already_final: upload_id={} (exact idempotent replay, row left untouched)", upload_id);
            return Ok(());
        }
    }
    anyhow::bail!(
        "completed_row_immutable: upload_id={} wrote no row because its terminal state or immutable identity differs",
        upload_id
    )
}

/// The schema makes `upload_id`, not a content digest, the manifest
/// registry identity.  Distinct uploads may share one CAS object/digest but
/// each owns a different `manifests/{robot}/{client_upload_id}.json` object,
/// and GC reaches that object only through this row.
const MANIFEST_INSERT_SQL: &str = "INSERT INTO manifests (upload_id, file_sha256, size_bytes, content_type, cas_key, manifest_s3_path, storage_path) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (upload_id) DO NOTHING";

/// The admin API resolves manifests only through their upload owner.  A
/// digest lookup is ambiguous by design because distinct
/// uploads of identical bytes share `file_sha256` and the CAS data key.
/// Register one upload's manifest path before the S3 manifest object is put.
///
/// The caller still holds the upload's cross-process advisory lock.  This
/// transaction additionally locks the authoritative `uploads` row, verifies
/// that the CAS metadata agrees with the immutable reservation, and either
/// inserts the per-upload registry row or accepts only an exact replay.  No
/// pool/query/constraint error is swallowed: completion cannot be reported
/// unless this function succeeds.
async fn pg_register_manifest(
    pg: &Pool,
    upload_id: &str,
    file_sha256: &str,
    size_bytes: i64,
    cas_key: &str,
    manifest_s3_path: &str,
    storage_path: &str,
) -> anyhow::Result<String> {
    let mut client = pg.get().await?;
    let transaction = client.transaction().await?;
    let upload = transaction
        .query_opt(
            "SELECT id, file_size_bytes, expected_sha256, storage_path, content_type FROM uploads WHERE client_upload_id=$1 FOR UPDATE",
            &[&upload_id],
        )
        .await?
        .ok_or_else(|| anyhow::anyhow!("manifest upload owner not found for client_upload_id={}", upload_id))?;
    let owner_id: Uuid = upload.get(0);
    let owner_size: i64 = upload.get(1);
    let owner_digest: Option<String> = upload.get(2);
    let owner_storage_path: String = upload.get(3);
    // `content_type` is deliberately not part of the upload's immutable
    // identity tuple.  A replay therefore uses the value already reserved in
    // `uploads`, never a restatement from the retrying request.
    let owner_content_type: Option<String> = upload.get(4);
    let owner_content_type = owner_content_type.unwrap_or_default();
    if owner_size != size_bytes
        || !owner_digest
            .as_deref()
            .is_some_and(|digest| digest.eq_ignore_ascii_case(file_sha256))
        || owner_storage_path != storage_path
    {
        anyhow::bail!(
            "manifest metadata conflicts with immutable upload reservation for client_upload_id={}",
            upload_id
        );
    }

    let rows_written = transaction
        .execute(
            MANIFEST_INSERT_SQL,
            &[
                &owner_id,
                &file_sha256,
                &size_bytes,
                &owner_content_type,
                &cas_key,
                &manifest_s3_path,
                &storage_path,
            ],
        )
        .await?;
    if rows_written == 0 {
        let existing = transaction
            .query_one(
                "SELECT file_sha256, size_bytes, content_type, cas_key, manifest_s3_path, storage_path FROM manifests WHERE upload_id=$1",
                &[&owner_id],
            )
            .await?;
        let existing_digest: String = existing.get(0);
        let existing_size: i64 = existing.get(1);
        let existing_content_type: Option<String> = existing.get(2);
        let existing_cas_key: String = existing.get(3);
        let existing_manifest_path: String = existing.get(4);
        let existing_storage_path: String = existing.get(5);
        if !existing_digest.eq_ignore_ascii_case(file_sha256)
            || existing_size != size_bytes
            || existing_content_type.unwrap_or_default() != owner_content_type
            || existing_cas_key != cas_key
            || existing_manifest_path != manifest_s3_path
            || existing_storage_path != storage_path
        {
            anyhow::bail!(
                "manifest registry is immutable and conflicts with replay for client_upload_id={}",
                upload_id
            );
        }
    }
    transaction.commit().await?;
    Ok(owner_content_type)
}

#[cfg(test)]
mod tests {
    use super::robot_auth::{AuthenticatedRobot, AuthenticatedRobotSource};
    use super::security_profile::MIN_RELEASE_TOKEN_BYTES;
    use super::{
        authorized_robot_filter, cas_manifest_document, empty_to_null,
        enforce_robot_certificate_claim, enforce_robot_resource_owner, evaluate_auth,
        finalize_completion, health_checks, healthcheck_response_is_ready,
        immutable_identity_conflicts, is_payload_upload_rpc, manifest_object_key, normalize_sha256,
        parse_s3_storage_path, profile_configuration_error, read_healthcheck_status_line,
        readiness_status, resolve_max_inflight_parts, robot_id_from_certificate_der,
        robot_id_from_spiffe_uri, s3_is_ready, validate_robot_id, validate_upload_payload_budget,
        AuthOutcome, CertificateRobotIdError, ImmutableUploadIdentity, RobotIdError,
        SharedTokenPolicy, UploadResponse, UploadTransportAdmission, COMPLETED_UPLOAD_UPSERT_SQL,
        DEFAULT_GLOBAL_CONCURRENCY, DEFAULT_MAX_INFLIGHT_PARTS, DEV_DEFAULT_TOKEN,
        MANIFEST_INSERT_SQL, MAX_INFLIGHT_PARTS_CEILING, MAX_INGEST_UPLOAD_PAYLOAD_BYTES,
        MAX_ROBOT_ID_LEN, MEBIBYTE, POSTGRES_READINESS_QUERY, REFERENCE_INGEST_CONTAINER_BYTES,
        RESERVE_UPLOAD_INSERT_SQL, ROBOT_SPIFFE_URI_PREFIX, UPLOAD_SMALL_RPC_PATH,
        UPLOAD_STREAM_RPC_PATH,
    };
    use super::{resolve_auth_recheck_interval, strip_bearer_scheme};
    use crate::security_profile::IngestSecurityProfile;
    use axum::http::StatusCode;
    use base64ct::{Base64, Encoding};
    use tonic::Code;

    // A realistic 64-char lowercase hex sha256 digest.
    const SHA_A: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    // A different valid digest.
    const SHA_B: &str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
    // Self-signed test leaf with CN=display-name-not-identity and exactly one
    // URI SAN: spiffe://robot-vault/robots/rover-7. The parser tests the DER
    // structure only; rustls performs trust/validity verification in the live
    // mTLS connection before Tonic exposes these bytes.
    const ROBOT_CERT_DER_B64: &str = "MIIDWzCCAkOgAwIBAgIUQV8WpB3Eb4Wrw5yJaMVjHOuX/zUwDQYJKoZIhvcNAQELBQAwJDEiMCAGA1UEAwwZZGlzcGxheS1uYW1lLW5vdC1pZGVudGl0eTAeFw0yNjA4MDkxMDA2MTBaFw0yNjA4MTExMDA2MTBaMCQxIjAgBgNVBAMMGWRpc3BsYXktbmFtZS1ub3QtaWRlbnRpdHkwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDFP1jI3rXgtMOuMhQaH6rEMu6oHdr5srxgOrpdCbSMshG0KzLK5DriPzh6xdEzYkcRtaOWKKHB8OORi9MTAkM4NgGy/3ZfIBq6NCQC5gRxyoO0aCUTZgd7jCcdCAAJbJsJsbCUOCmFbX6dlO2dP213x87FmBPesnape93uI9AcrGq4TukoYTMkDr/B0ttBUF050fZAXskLC3RZIt7FaseHG+7AVc4QX1/TfOlFIN+bmgG1zePjGxvodkvgKzLBRHW82xYYWqOkWNQxujxsSCtkWHud9U7XdLWG92b7xRQDKSIUUZobOT00bwQa0qJc/YvHBjxQy4MCmEd4hXr95XNZAgMBAAGjgYQwgYEwHQYDVR0OBBYEFLJ+C2CiBDJ9q5MbnxWnwH+gaDGLMB8GA1UdIwQYMBaAFLJ+C2CiBDJ9q5MbnxWnwH+gaDGLMA8GA1UdEwEB/wQFMAMBAf8wLgYDVR0RBCcwJYYjc3BpZmZlOi8vcm9ib3QtdmF1bHQvcm9ib3RzL3JvdmVyLTcwDQYJKoZIhvcNAQELBQADggEBAIdZ3v27kWP+p/Wfkpe9F8zQq7F6SOt+DlSVwEYHavajmnx7ap80qqT9hmqvUnAZexjJVi4HVNFk9Upm/TXT9AI3lOQcyGOebqUr3mUjBkeL628TXupek4+NjPw3o+xFemLZn226ru7E/bEwTfBRyfN7CX5VRvzij6s4xvgPdu4zTlCKSRDorHA9T/hNB0ug3pitXMLWimUVcw6EiZad7FtHWg5GDyBql+AVzL5RZBlQ7LXC6rEB4vMdfLhEuxabcUjEaodyqLp9f3zi/Oyzyxy0OckA/D7OErtTPW6CYzJPF8tDl1gBFZSjwRqKjTqreQqSOzMFMicW/NOWCPIryZw=";

    #[test]
    fn readiness_requires_both_storage_and_postgres() {
        assert_eq!(readiness_status(true, true), StatusCode::OK);
        assert_eq!(
            readiness_status(false, true),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(
            readiness_status(true, false),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(
            readiness_status(false, false),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(POSTGRES_READINESS_QUERY, "SELECT 1");
    }

    #[tokio::test]
    async fn readiness_rejects_missing_s3_configuration() {
        assert!(!s3_is_ready(None, None).await);
    }

    #[test]
    fn postgres_uri_percent_decodes_delimiter_rich_password() {
        let config: tokio_postgres::Config =
            "postgresql://robotvault:p%40ss%3A%2F%3F%23%25%20value@postgres:5432/robotvault"
                .parse()
                .expect("the release URI must be accepted by the ingest driver");
        assert_eq!(config.get_user(), Some("robotvault"));
        assert_eq!(config.get_password(), Some(b"p@ss:/?#% value".as_slice()));
        assert_eq!(config.get_dbname(), Some("robotvault"));
    }

    // The `mtls` row of `profile_configuration_error`'s profile table also
    // requires a provisioning token distinct from the auth token.
    #[test]
    fn mtls_profile_requires_mtls_and_a_non_default_token() {
        let release_token = "f".repeat(MIN_RELEASE_TOKEN_BYTES);
        let provisioning = "a".repeat(MIN_RELEASE_TOKEN_BYTES);
        let prod = IngestSecurityProfile::Mtls;
        let ok = |tls, mtls, auth| {
            profile_configuration_error(
                prod,
                tls,
                mtls,
                SharedTokenPolicy::Deny,
                auth,
                Some(&provisioning),
            )
        };

        assert_eq!(ok(true, true, Some(&release_token)), None);
        // `mtls` without mTLS is refused.
        assert!(ok(true, false, Some(&release_token)).is_some());
        // TLS off is refused whether or not mTLS is requested.
        assert!(ok(false, false, Some(&release_token)).is_some());
        // The auth token must be present, non-blank, non-default and long enough.
        assert!(ok(true, true, None).is_some());
        assert!(ok(true, true, Some("   ")).is_some());
        assert!(ok(true, true, Some(DEV_DEFAULT_TOKEN)).is_some());
        assert!(ok(true, true, Some(TOKEN)).is_some());
        let mut control_token = "f".repeat(MIN_RELEASE_TOKEN_BYTES);
        control_token.replace_range(1..2, "\n");
        assert!(ok(true, true, Some(&control_token)).is_some());

        // The provisioning token is required and must differ from the
        // fleet token — the provisioning boundary rests on secret separation.
        assert!(profile_configuration_error(
            prod,
            true,
            true,
            SharedTokenPolicy::Deny,
            Some(&release_token),
            None,
        )
        .is_some());
        assert!(profile_configuration_error(
            prod,
            true,
            true,
            SharedTokenPolicy::Deny,
            Some(&release_token),
            Some(&release_token),
        )
        .is_some());

        // `mtls` may not accept the shared token on the data plane.
        assert!(profile_configuration_error(
            prod,
            true,
            true,
            SharedTokenPolicy::Allow,
            Some(&release_token),
            Some(&provisioning),
        )
        .is_some());
    }

    #[test]
    fn mtls_without_tls_is_rejected_in_every_profile() {
        // An internally inconsistent transport request is never a warning,
        // including in `dev`, where everything else is optional.
        for profile in [
            IngestSecurityProfile::Dev,
            IngestSecurityProfile::Token,
            IngestSecurityProfile::Mtls,
        ] {
            assert!(profile_configuration_error(
                profile,
                false,
                true,
                SharedTokenPolicy::Allow,
                None,
                None,
            )
            .is_some());
        }
        // Bare `dev` boots with nothing configured.
        assert_eq!(
            profile_configuration_error(
                IngestSecurityProfile::Dev,
                false,
                false,
                SharedTokenPolicy::Allow,
                None,
                None,
            ),
            None
        );
    }

    #[test]
    fn unary_upload_requires_a_client_digest() {
        let error = super::resolve_unary_expected_sha256("", SHA_A)
            .expect_err("UploadSmall must not derive its own source claim");
        assert_eq!(error.code(), Code::InvalidArgument);
        assert!(error.message().contains("requires expected_sha256"));
    }

    // ---- Bounded S3 part pipelining ------------------------------------------
    //
    // The concurrency itself needs a live S3 (tests/e2e drives the real MPU
    // path); these pin how many parts may be in flight.

    #[test]
    fn inflight_bound_defaults_when_unset_or_unparseable() {
        assert_eq!(resolve_max_inflight_parts(None), DEFAULT_MAX_INFLIGHT_PARTS);
        assert_eq!(
            resolve_max_inflight_parts(Some("")),
            DEFAULT_MAX_INFLIGHT_PARTS
        );
        assert_eq!(
            resolve_max_inflight_parts(Some("lots")),
            DEFAULT_MAX_INFLIGHT_PARTS
        );
        assert_eq!(
            resolve_max_inflight_parts(Some("-1")),
            DEFAULT_MAX_INFLIGHT_PARTS
        );
    }

    #[test]
    fn inflight_bound_honours_a_sane_operator_value() {
        assert_eq!(resolve_max_inflight_parts(Some("8")), 8);
        // Whitespace from an env file / compose `KEY= 8` is tolerated.
        assert_eq!(resolve_max_inflight_parts(Some("  8 ")), 8);
    }

    #[test]
    fn inflight_bound_is_never_zero_and_never_unbounded() {
        // 0 would mean "never upload a part"; the receive loop would deadlock
        // waiting for a slot it can never get.
        assert_eq!(resolve_max_inflight_parts(Some("0")), 1);
        // Every in-flight part pins a chunk of memory per concurrent stream, so
        // an over-large value is clamped, not honoured.
        assert_eq!(
            resolve_max_inflight_parts(Some("100000")),
            MAX_INFLIGHT_PARTS_CEILING
        );
    }

    #[test]
    fn default_upload_limits_stay_below_the_container_payload_budget() {
        let payload =
            validate_upload_payload_budget(DEFAULT_GLOBAL_CONCURRENCY, DEFAULT_MAX_INFLIGHT_PARTS)
                .expect("shipped upload settings must satisfy the memory budget");

        // 8 streams x (4 accepted 8 MiB parts + one possible 50 MiB decoded
        // message) = 656 MiB. The handler never moves the oversized current
        // message into an S3 task.
        assert_eq!(payload, 656 * MEBIBYTE);
        assert!(payload <= MAX_INGEST_UPLOAD_PAYLOAD_BYTES);
        assert!(payload < REFERENCE_INGEST_CONTAINER_BYTES);
    }

    #[test]
    fn configured_upload_limits_fail_above_the_upload_payload_budget() {
        // 8 streams x (9 accepted 8 MiB parts + one possible 50 MiB decoded
        // message) = 976 MiB, still below the 1 GiB budget.
        assert_eq!(validate_upload_payload_budget(8, 9), Ok(976 * MEBIBYTE));
        // One additional retained part per stream raises the bound to 1040
        // MiB, so startup must reject it.
        let error = validate_upload_payload_budget(8, 10)
            .expect_err("a configuration above the payload budget must fail");
        assert!(error.contains("can hold 1040 MiB"));
        assert!(error.contains("limit is 1024 MiB"));
        assert!(
            validate_upload_payload_budget(
                super::MAX_GLOBAL_CONCURRENCY,
                MAX_INFLIGHT_PARTS_CEILING,
            )
            .is_err(),
            "individually valid maxima must not bypass the combined budget"
        );
        assert!(validate_upload_payload_budget(usize::MAX, 1).is_err());
    }

    #[tokio::test]
    async fn upload_transport_admission_is_shared_fail_fast_and_payload_only() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        use tonic::codegen::{http, BoxFuture, Service};

        #[derive(Clone)]
        struct DecodeProbe {
            calls: Arc<AtomicUsize>,
        }

        impl tonic::server::NamedService for DecodeProbe {
            const NAME: &'static str = "test.DecodeProbe";
        }

        impl Service<http::Request<()>> for DecodeProbe {
            type Response = http::Response<tonic::body::BoxBody>;
            type Error = std::convert::Infallible;
            type Future = BoxFuture<Self::Response, Self::Error>;

            fn poll_ready(
                &mut self,
                _context: &mut std::task::Context<'_>,
            ) -> std::task::Poll<Result<(), Self::Error>> {
                std::task::Poll::Ready(Ok(()))
            }

            fn call(&mut self, _request: http::Request<()>) -> Self::Future {
                self.calls.fetch_add(1, Ordering::SeqCst);
                Box::pin(async { Ok(http::Response::new(tonic::codegen::empty_body())) })
            }
        }

        fn assert_named<T: tonic::server::NamedService>() {}

        let calls = Arc::new(AtomicUsize::new(0));
        let admission = UploadTransportAdmission::new(
            DecodeProbe {
                calls: calls.clone(),
            },
            1,
        );
        assert_named::<UploadTransportAdmission<DecodeProbe>>();

        let mut first_connection = admission.clone();
        std::future::poll_fn(|context| first_connection.poll_ready(context))
            .await
            .unwrap();
        let first_inflight = first_connection.call(
            http::Request::builder()
                .uri(UPLOAD_SMALL_RPC_PATH)
                .body(())
                .unwrap(),
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        let mut second_connection = admission.clone();
        std::future::poll_fn(|context| second_connection.poll_ready(context))
            .await
            .unwrap();
        let overloaded = second_connection
            .call(
                http::Request::builder()
                    .uri(UPLOAD_STREAM_RPC_PATH)
                    .body(())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            overloaded
                .headers()
                .get("grpc-status")
                .and_then(|value| value.to_str().ok()),
            Some("8"),
            "a second connection must fail before invoking tonic's decoder"
        );
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        // Small control requests remain available while an upload owns the
        // only payload permit.
        let control = second_connection.call(
            http::Request::builder()
                .uri("/robotics.upload.v1.UploadService/GetUploadStatus")
                .body(())
                .unwrap(),
        );
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        drop(control);

        drop(first_inflight);
        let admitted_after_release = second_connection.call(
            http::Request::builder()
                .uri(UPLOAD_STREAM_RPC_PATH)
                .body(())
                .unwrap(),
        );
        assert_eq!(calls.load(Ordering::SeqCst), 3);
        drop(admitted_after_release);

        assert!(is_payload_upload_rpc(UPLOAD_STREAM_RPC_PATH));
        assert!(is_payload_upload_rpc(UPLOAD_SMALL_RPC_PATH));
        assert!(!is_payload_upload_rpc(
            "/robotics.upload.v1.UploadService/HealthCheck"
        ));

        let source = include_str!("main.rs");
        let wrapper = source
            .find(
                "let upload_service = UploadTransportAdmission::new(upload_service, global_limit);",
            )
            .expect("upload service transport admission wrapper");
        let router = source[wrapper..]
            .find(".add_service(upload_service)")
            .map(|offset| wrapper + offset)
            .expect("upload service router registration");
        assert!(
            wrapper < router,
            "admission must wrap before router cloning"
        );
        let unary_gate = ["try_acquire_per_robot", "(&self.state"].concat();
        let streaming_gate = ["try_acquire_per_robot", "(state,"].concat();
        assert_eq!(
            source.matches(&unary_gate).count()
                + include_str!("resumable.rs")
                    .matches(&streaming_gate)
                    .count(),
            2,
            "both payload handlers must fail fast at the per-robot gate"
        );
    }

    #[test]
    fn upload_concurrency_defaults_and_accepts_only_bounded_positive_values() {
        assert_eq!(
            super::resolve_concurrency_limit(
                "GLOBAL_CONCURRENCY",
                None,
                super::DEFAULT_GLOBAL_CONCURRENCY,
                super::MAX_GLOBAL_CONCURRENCY,
            ),
            Ok(super::DEFAULT_GLOBAL_CONCURRENCY)
        );
        assert_eq!(
            super::resolve_concurrency_limit("GLOBAL_CONCURRENCY", Some(" 16 "), 8, 64),
            Ok(16)
        );
        assert_eq!(
            super::resolve_concurrency_limit(
                "PER_ROBOT_CONCURRENCY",
                Some("16"),
                super::DEFAULT_PER_ROBOT_CONCURRENCY,
                super::MAX_PER_ROBOT_CONCURRENCY,
            ),
            Ok(super::MAX_PER_ROBOT_CONCURRENCY)
        );
        assert!(super::resolve_concurrency_limit("GLOBAL_CONCURRENCY", Some("0"), 8, 64).is_err());
        assert!(super::resolve_concurrency_limit("GLOBAL_CONCURRENCY", Some("65"), 8, 64).is_err());
        assert!(
            super::resolve_concurrency_limit("GLOBAL_CONCURRENCY", Some("many"), 8, 64).is_err()
        );
        assert!(super::resolve_concurrency_limit("GLOBAL_CONCURRENCY", Some(""), 8, 64).is_err());
        assert!(
            super::resolve_concurrency_limit("PER_ROBOT_CONCURRENCY", Some("17"), 2, 16).is_err()
        );
    }

    #[test]
    fn streaming_timeouts_default_and_reject_non_finite_or_unbounded_values() {
        assert_eq!(
            super::resolve_timeout_seconds("TIMEOUT", None, 10.0, 3600.0),
            Ok(std::time::Duration::from_secs(10))
        );
        assert_eq!(
            super::resolve_timeout_seconds("TIMEOUT", Some(" 2.5 "), 10.0, 3600.0),
            Ok(std::time::Duration::from_millis(2500))
        );
        for value in ["", "nope", "0", "-1", "NaN", "inf", "3601"] {
            assert!(
                super::resolve_timeout_seconds("TIMEOUT", Some(value), 10.0, 3600.0).is_err(),
                "{value:?} must not disable or unbound a stream timeout"
            );
        }
    }

    #[test]
    fn admin_and_grpc_servers_are_supervised_by_main() {
        let source = include_str!("main.rs");
        let start = source
            .find("    // Keep both serving futures in the main task")
            .expect("server supervision setup");
        let end = source[start..]
            .find("\nfn now_ts()")
            .map(|offset| start + offset)
            .expect("end of main");
        let serving = &source[start..end];
        assert!(serving.contains("tokio::select!"));
        assert!(serving.contains("admin server exited unexpectedly"));
        assert!(serving.contains("gRPC server exited unexpectedly"));
        assert!(!serving.contains("tokio::spawn"));
    }

    // ---- Completed-row write gates the RPC's success response ----------------

    fn sample_completed_response() -> UploadResponse {
        UploadResponse {
            upload_id: "robot-1-123".into(),
            status: "completed".into(),
            bytes_received: 1000,
            final_hash: SHA_A.into(),
            completed_at: None,
            storage_path: "s3://bucket/key".into(),
            error_message: String::new(),
        }
    }

    #[test]
    fn completed_row_write_ok_returns_completed_response() {
        // DB write confirmed -> the RPC returns the pre-built completed response.
        let resp = sample_completed_response();
        let out = finalize_completion(resp, Ok::<(), String>(()));
        let got = out.expect("a confirmed completed-row write must yield the response");
        assert_eq!(got.status, "completed");
        assert_eq!(got.upload_id, "robot-1-123");
    }

    #[test]
    fn completed_row_write_err_returns_internal_not_completed() {
        // DB write failed -> the RPC must NOT report completed; it returns
        // Status::internal so the client retries and the object is not stranded.
        let resp = sample_completed_response();
        let out = finalize_completion(resp, Err::<(), String>("pg pool exhausted".into()));
        let status =
            out.expect_err("a failed completed-row write must NOT yield a completed response");
        assert_eq!(status.code(), Code::Internal);
        assert!(
            status.message().contains("completed_row_write_failed"),
            "the client-visible error should name the completed-row write failure, got: {}",
            status.message()
        );
    }

    // ---- Capture-metadata intake -----------------------------------------------
    //
    // The five capture columns must reach the `uploads` table through all
    // both SQL writers (reservation/completed). This pins WHERE in each
    // statement they must appear, mirroring the immutability tripwires above so
    // a future edit that drops one from an INSERT/SET list fails here
    // instead of only showing up as a silently-NULL column in production.

    const CAPTURE_COLUMNS: [&str; 5] = [
        "operator",
        "site",
        "permission_ref",
        "session_tags",
        "task_label",
    ];

    #[test]
    fn reservation_insert_sql_carries_every_capture_column() {
        // DO NOTHING has no SET list, so INSERT-only is the only possible
        // treatment here -- unlike the completed upsert below, there is
        // nothing to distinguish.
        for col in CAPTURE_COLUMNS {
            assert!(
                RESERVE_UPLOAD_INSERT_SQL.contains(col),
                "capture column `{}` must be in the reservation INSERT list: {}",
                col,
                RESERVE_UPLOAD_INSERT_SQL
            );
        }
    }

    #[test]
    fn completed_upsert_sql_carries_every_capture_column_in_insert_and_set() {
        // The completed upsert restates these five columns in its SET list
        // as well as its INSERT list. The reservation is the first writer;
        // the completion write records the final attempt context while its
        // immutable-identity guard prevents cross-upload restatement.
        let insert_list = COMPLETED_UPLOAD_UPSERT_SQL
            .split(") VALUES")
            .next()
            .expect("upsert must have an INSERT column list");
        let set_clause = &COMPLETED_UPLOAD_UPSERT_SQL[COMPLETED_UPLOAD_UPSERT_SQL
            .find("DO UPDATE SET")
            .expect("upsert must have a DO UPDATE SET clause")..];
        for col in CAPTURE_COLUMNS {
            assert!(
                insert_list.contains(col),
                "capture column `{}` must be in the completed-row INSERT list: {}",
                col,
                COMPLETED_UPLOAD_UPSERT_SQL
            );
            assert!(
                set_clause.contains(col),
                "capture column `{}` must be restated in the completed-row DO UPDATE SET list \
                 (upload_small has no other writer for it): {}",
                col,
                set_clause
            );
        }
    }

    #[test]
    fn empty_capture_field_becomes_sql_null_not_empty_string() {
        // proto3 has no distinct "unset" for a string field -- "" IS the wire
        // default an operator-less robot config sends. This is what keeps
        // that from persisting as a visible-but-meaningless empty string.
        assert_eq!(empty_to_null(""), None);
        assert_eq!(empty_to_null("alice"), Some("alice"));
    }

    // ---- Shared-token auth ----

    const TOKEN: &str = "s3cr3t-ingest-token";

    #[test]
    fn no_expected_token_means_auth_disabled() {
        // Fail-open when INGEST_AUTH_TOKEN is unset/empty: upgrading the image
        // must not start rejecting a fleet that has no token configured yet.
        assert_eq!(evaluate_auth(None, None), AuthOutcome::Allowed);
        assert_eq!(evaluate_auth(Some(""), None), AuthOutcome::Allowed);
        assert_eq!(
            evaluate_auth(Some("   "), Some("anything")),
            AuthOutcome::Allowed
        );
    }

    #[test]
    fn missing_credential_is_rejected_when_a_token_is_configured() {
        assert_eq!(
            evaluate_auth(Some(TOKEN), None),
            AuthOutcome::MissingCredential
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("")),
            AuthOutcome::MissingCredential
        );
        // A bare scheme with no credential after it is missing, not invalid.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("Bearer ")),
            AuthOutcome::MissingCredential
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("Bearer")),
            AuthOutcome::MissingCredential
        );
    }

    #[test]
    fn wrong_credential_is_rejected() {
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("nope")),
            AuthOutcome::InvalidCredential
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("Bearer nope")),
            AuthOutcome::InvalidCredential
        );
        // Same length as the real token, so the length check can't be what
        // rejects it -- the byte comparison must.
        let same_len: String = TOKEN.chars().rev().collect();
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&same_len)),
            AuthOutcome::InvalidCredential
        );
        // The token itself is compared case-sensitively.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&TOKEN.to_uppercase())),
            AuthOutcome::InvalidCredential
        );
        // A prefix of the real token must not pass.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&TOKEN[..5])),
            AuthOutcome::InvalidCredential
        );
    }

    #[test]
    fn matching_credential_is_allowed_with_or_without_the_bearer_scheme() {
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(TOKEN)),
            AuthOutcome::Allowed
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&format!("Bearer {}", TOKEN))),
            AuthOutcome::Allowed
        );
        // The scheme name is case-insensitive (RFC 6750), the token is not.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&format!("bearer {}", TOKEN))),
            AuthOutcome::Allowed
        );
        // Surrounding whitespace from a hand-rolled client is tolerated.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some(&format!("  Bearer  {}  ", TOKEN))),
            AuthOutcome::Allowed
        );
    }

    #[test]
    fn multibyte_credential_does_not_panic() {
        // `evaluate_auth` splits the presented value to strip the scheme; a
        // shorter-than-scheme or non-ASCII value must not split a char boundary.
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("é")),
            AuthOutcome::InvalidCredential
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("Bea")),
            AuthOutcome::InvalidCredential
        );
        assert_eq!(
            evaluate_auth(Some(TOKEN), Some("Béarer token")),
            AuthOutcome::InvalidCredential
        );
        // A multi-byte token that happens to match is still accepted.
        assert_eq!(
            evaluate_auth(Some("tökén"), Some("Bearer tökén")),
            AuthOutcome::Allowed
        );
    }

    // ===== Robot-id shape rules =====
    //
    // The "is it in the `robots` table?" half needs a live Postgres, so it is
    // covered by tests/e2e/test_pipeline_e2e.py. These pin the DB-free half:
    // the ids that are refused before a lookup is even worth doing.

    #[test]
    fn a_registered_looking_robot_id_passes_the_shape_check() {
        assert_eq!(validate_robot_id("demo"), Ok("demo"));
        assert_eq!(
            validate_robot_id("e2e_test_0123456789"),
            Ok("e2e_test_0123456789")
        );
        // Surrounding spaces are trimmed, and the trimmed form is what gets looked up —
        // otherwise " demo" would be a different robot from "demo".
        assert_eq!(validate_robot_id("  demo  "), Ok("demo"));
    }

    #[test]
    fn an_empty_robot_id_is_refused() {
        assert_eq!(validate_robot_id(""), Err(RobotIdError::Missing));
        assert_eq!(validate_robot_id("   "), Err(RobotIdError::Missing));
    }

    #[test]
    fn the_unknown_sentinel_is_refused() {
        // A client may send this literal string when it cannot derive a robot
        // id. Accepting it would file every unidentifiable upload under one
        // fake robot.
        assert_eq!(
            validate_robot_id("unknown"),
            Err(RobotIdError::UnknownSentinel)
        );
        assert_eq!(
            validate_robot_id("UNKNOWN"),
            Err(RobotIdError::UnknownSentinel)
        );
        assert_eq!(
            validate_robot_id(" Unknown "),
            Err(RobotIdError::UnknownSentinel)
        );
        // A robot legitimately *named* something containing "unknown" is fine —
        // only the exact sentinel is refused.
        assert_eq!(validate_robot_id("unknown-2"), Ok("unknown-2"));
    }

    #[test]
    fn an_over_long_robot_id_is_refused_before_postgres_sees_it() {
        let at_limit = "r".repeat(MAX_ROBOT_ID_LEN);
        assert_eq!(validate_robot_id(&at_limit), Ok(at_limit.as_str()));
        let over = "r".repeat(MAX_ROBOT_ID_LEN + 1);
        assert_eq!(validate_robot_id(&over), Err(RobotIdError::TooLong));
        // VARCHAR(n) counts characters, not bytes: 50 two-byte chars fit.
        let multibyte = "é".repeat(MAX_ROBOT_ID_LEN);
        assert_eq!(validate_robot_id(&multibyte), Ok(multibyte.as_str()));
        assert_eq!(
            validate_robot_id(&"é".repeat(MAX_ROBOT_ID_LEN + 1)),
            Err(RobotIdError::TooLong)
        );
    }

    #[test]
    fn a_robot_id_with_a_path_or_control_character_is_refused() {
        // `robot_id` becomes a path segment (`storage_path`'s
        // `.join(robot_id)`) and an S3 object-key component
        // (`manifest_object_key`'s `format!("manifests/{}/{}.json", ...)`).
        // Only tokens that could act as a separator or control character, plus
        // the exact current/traversal segments, are refused.
        for bad in [
            "a/b", "a\\b", ".", "..", "a\0b", "a\tb", "a\nb", "a\u{7f}b", " demo\n",
        ] {
            assert_eq!(
                validate_robot_id(bad),
                Err(RobotIdError::InvalidCharset),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn robot_ids_with_space_punctuation_or_non_ascii_text_are_accepted() {
        // A certificate-derived id may contain a decoded space (see
        // `robot_spiffe_uri_decodes_one_canonical_path_segment`'s "rover
        // seven"), and non-ASCII text is a valid VARCHAR(50) value (see
        // `an_over_long_robot_id_is_refused_before_postgres_sees_it`'s "é"
        // fixture) -- neither is a path or SQL injection risk here.
        for good in [
            "robot-01",
            "robot_01",
            "arm.left.02",
            "a..b",
            ".hidden",
            "robot.",
            "R2D2",
            "rover seven",
            "rôver",
        ] {
            assert_eq!(validate_robot_id(good), Ok(good), "{good}");
        }
    }

    #[test]
    fn invalid_robot_id_message_does_not_echo_control_bearing_input() {
        let raw = "rover-7\nforged-log-line";
        let message = RobotIdError::InvalidCharset.message(raw);
        assert!(!message.contains(raw));
        assert!(
            !message.chars().any(|c| c.is_ascii_control()),
            "control characters must not be reflected into the client-visible error: {message:?}"
        );
    }

    #[test]
    fn robot_id_rejection_reasons_have_stable_distinct_labels() {
        let labels = [
            RobotIdError::Missing.label(),
            RobotIdError::UnknownSentinel.label(),
            RobotIdError::TooLong.label(),
            RobotIdError::InvalidCharset.label(),
            RobotIdError::NotRegistered.label(),
        ];
        let mut sorted = labels.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(
            sorted.len(),
            labels.len(),
            "metric labels must be distinct: {:?}",
            labels
        );
        // The unregistered message has to tell an operator how to fix it.
        let msg = RobotIdError::NotRegistered.message("rover-7");
        assert!(msg.contains("rover-7"), "{}", msg);
        assert!(msg.contains("/admin/robots"), "{}", msg);
    }

    #[test]
    fn missing_robot_registry_schema_fails_closed() {
        let status = super::robot_registry_query_error(Some("42P01"));
        assert_eq!(status.code(), Code::Unavailable);
        assert!(status.message().contains("apply database migrations"));
        assert!(status.message().contains("schema_missing"));

        // The gate is an authorization-shaped check, so a Postgres failure must
        // become `Status::unavailable` and never an allow: "the database is
        // down" must not be the way to bypass it. Asserted against the source
        // of the gate itself, because the failure mode is a missing branch.
        let source = include_str!("main.rs");
        let start = source
            .find("async fn lookup_upload_target")
            .expect("fleet-registry gate");
        let end = source[start..]
            .find("async fn authorize_upload_target")
            .map(|offset| start + offset)
            .expect("end of fleet-registry gate");
        let registry_path = &source[start..end];
        assert!(
            registry_path.contains("Err(robot_registry_query_error(sqlstate.as_deref()))"),
            "the registry gate must convert a query error into a refusal"
        );
        assert!(!registry_path.contains("accepting robot_id"));
    }

    #[test]
    fn per_robot_credential_verification_fails_closed_too() {
        // The same property for the per-robot credential lookup: a pool or query error on
        // the credential path is `UNAVAILABLE`, never an authenticated caller.
        // This is the branch that would otherwise make "stop Postgres" a way to
        // upload as any robot.
        let source = include_str!("main.rs");
        let start = source
            .find("async fn resolve_request_identity")
            .expect("credential resolution");
        let end = source[start..]
            .find("async fn recheck_stream_identity")
            .map(|offset| start + offset)
            .expect("end of credential resolution");
        let resolution = &source[start..end];
        assert!(
            resolution.contains("Status::unavailable(\"robot_credential_registry_unavailable\")"),
            "a credential-lookup failure must refuse, not fall through"
        );
        // ...and every *rejection* is the one generic message, so a well-formed
        // unknown credential is indistinguishable from garbage.
        assert_eq!(
            resolution
                .matches("Status::unauthenticated(\"invalid ingest auth token\")")
                .count(),
            2,
            "both the shape and the lookup rejection must use the generic message"
        );
    }

    #[test]
    fn the_upload_gate_distinguishes_lifecycle_refusal_from_policy_refusal() {
        // Two different statuses on purpose, and the
        // ordering is load-bearing: the lifecycle check runs first so a
        // disabled robot is refused even under `INGEST_SHARED_TOKEN_POLICY=allow`.
        let source = include_str!("main.rs");
        let start = source
            .find("async fn authorize_upload_target")
            .expect("upload authorization gate");
        let end = source[start..]
            .find("// ========== Release robot certificate identity")
            .map(|offset| start + offset)
            .expect("end of upload authorization gate");
        let gate = &source[start..end];
        let lifecycle = gate.find("robot_not_enrolled").expect("lifecycle refusal");
        let policy = gate
            .find("shared_token_policy")
            .expect("shared-token policy refusal");
        assert!(
            lifecycle < policy,
            "the lifecycle state check must precede the shared-token policy check"
        );
        assert!(gate.contains("Status::failed_precondition"));
        assert!(gate.contains("Status::unauthenticated(\"invalid ingest auth token\")"));
    }

    // ---- Release certificate identity --------------------------------------

    #[test]
    fn leaf_certificate_identity_comes_from_uri_san_not_common_name() {
        let der = Base64::decode_vec(ROBOT_CERT_DER_B64).expect("valid DER fixture encoding");
        assert_eq!(
            robot_id_from_certificate_der(&der),
            Ok("rover-7".to_string())
        );
    }

    #[test]
    fn robot_spiffe_uri_decodes_one_canonical_path_segment() {
        assert_eq!(
            robot_id_from_spiffe_uri("spiffe://robot-vault/robots/rover-7"),
            Ok(Some("rover-7".to_string()))
        );
        assert_eq!(
            robot_id_from_spiffe_uri("spiffe://robot-vault/robots/rover%20seven"),
            Ok(Some("rover seven".to_string()))
        );
        assert_eq!(
            robot_id_from_spiffe_uri("spiffe://another-domain/workload/rover-7"),
            Ok(None),
            "unrelated URI SANs are ignored"
        );
        assert!(ROBOT_SPIFFE_URI_PREFIX.ends_with("/robots/"));
    }

    #[test]
    fn malformed_robot_spiffe_uri_is_rejected() {
        for uri in [
            "spiffe://robot-vault/robots/",
            "spiffe://robot-vault/robots/a/b",
            "spiffe://robot-vault/robots/a?role=admin",
            "spiffe://robot-vault/robots/a#fragment",
            "spiffe://robot-vault/robots/bad%2",
            "spiffe://robot-vault/robots/unknown",
        ] {
            assert_eq!(
                robot_id_from_spiffe_uri(uri),
                Err(CertificateRobotIdError::MalformedRobotUriSan),
                "{uri}"
            );
        }
    }

    // ---- The ownership plumbing, driven from BOTH identity sources ----------
    //
    // Every assertion below is run once per `AuthenticatedRobot` source, so a
    // bearer-authenticated robot can never reach these functions as `None`
    // unnoticed. Parameterising the source makes the bearer and certificate
    // checks the *same* assertions rather than two half-checked paths.
    fn both_identity_sources(robot_id: &str) -> [AuthenticatedRobot; 2] {
        [
            AuthenticatedRobot::from_certificate(robot_id),
            AuthenticatedRobot::from_bearer(robot_id),
        ]
    }

    #[test]
    fn authenticated_robot_claim_must_equal_declared_robot() {
        // Bearer and certificate claims, one body.
        for identity in both_identity_sources("rover-7") {
            assert!(enforce_robot_certificate_claim(Some(&identity), " rover-7 ").is_ok());
            let error = enforce_robot_certificate_claim(Some(&identity), "rover-8")
                .expect_err("an authenticated robot must not impersonate a second robot");
            assert_eq!(
                error.code(),
                Code::PermissionDenied,
                "source {:?}",
                identity.source
            );
        }
        assert!(
            enforce_robot_certificate_claim(None, "rover-7").is_ok(),
            "the operator class keeps its historical unscoped behaviour"
        );
    }

    #[test]
    fn an_empty_list_filter_is_narrowed_to_the_authenticated_robot() {
        // `ListUploads` with an empty `robot_id` must not return the whole fleet
        // to a bearer-authenticated robot.
        for identity in both_identity_sources("rover-7") {
            assert_eq!(
                authorized_robot_filter(Some(&identity), "").unwrap(),
                Some("rover-7".to_string()),
                "source {:?} must not be able to list the fleet",
                identity.source
            );
        }
    }

    #[test]
    fn a_cross_robot_list_filter_is_refused_for_both_sources() {
        for identity in both_identity_sources("rover-7") {
            assert_eq!(
                authorized_robot_filter(Some(&identity), " rover-7 ").unwrap(),
                Some("rover-7".to_string())
            );
            let error = authorized_robot_filter(Some(&identity), "rover-8")
                .expect_err("a scoped list filter must not cross robot ownership");
            assert_eq!(error.code(), Code::PermissionDenied);
        }
    }

    #[test]
    fn upload_row_ownership_is_enforced_for_both_sources() {
        // `GetUploadStatus` on another robot's upload id, for both sources.
        for identity in both_identity_sources("rover-7") {
            assert!(enforce_robot_resource_owner(Some(&identity), "rover-7").is_ok());
            let error = enforce_robot_resource_owner(Some(&identity), "rover-8")
                .expect_err("status/resume must not expose another robot's upload");
            assert_eq!(error.code(), Code::PermissionDenied);
        }
    }

    #[test]
    fn operator_class_upload_metadata_reads_keep_their_existing_scope() {
        assert_eq!(authorized_robot_filter(None, "").unwrap(), None);
        assert_eq!(
            authorized_robot_filter(None, "rover-8").unwrap(),
            Some("rover-8".to_string())
        );
        assert!(enforce_robot_resource_owner(None, "rover-8").is_ok());
    }

    #[test]
    fn the_two_identity_sources_are_interchangeable_to_every_guard() {
        // The mutation this kills: a guard that inspects `source` and takes a
        // shortcut for one of them. Same id, same answer, both ways.
        let certificate = AuthenticatedRobot::from_certificate("rover-7");
        let bearer = AuthenticatedRobot::from_bearer("rover-7");
        assert_eq!(certificate.robot_id(), bearer.robot_id());
        assert_ne!(certificate.source, bearer.source);
        assert_eq!(
            certificate.source,
            AuthenticatedRobotSource::ClientCertificate
        );
        assert_eq!(bearer.source, AuthenticatedRobotSource::BearerCredential);
        for requested in ["", "rover-7", " rover-7 ", "rover-8"] {
            let from_certificate = authorized_robot_filter(Some(&certificate), requested);
            let from_bearer = authorized_robot_filter(Some(&bearer), requested);
            match (from_certificate, from_bearer) {
                (Ok(left), Ok(right)) => assert_eq!(left, right, "{requested:?}"),
                (Err(left), Err(right)) => {
                    assert_eq!(left.code(), right.code(), "{requested:?}");
                    assert_eq!(left.message(), right.message(), "{requested:?}");
                }
                _ => panic!("the two identity sources diverged on {requested:?}"),
            }
        }
    }

    // ---- The re-check interval is a ceiling, not a default ----------------

    #[test]
    fn the_credential_recheck_interval_can_only_be_tightened() {
        assert_eq!(
            resolve_auth_recheck_interval(None),
            std::time::Duration::from_secs(30)
        );
        assert_eq!(
            resolve_auth_recheck_interval(Some("1")),
            std::time::Duration::from_secs(1)
        );
        for loosened in ["31", "3000", "86400"] {
            assert_eq!(
                resolve_auth_recheck_interval(Some(loosened)),
                std::time::Duration::from_secs(30),
                "{loosened} must not weaken the 30 s revocation guarantee"
            );
        }
        for nonsense in ["0", "-5", "", "abc", "  "] {
            assert_eq!(
                resolve_auth_recheck_interval(Some(nonsense)),
                std::time::Duration::from_secs(30),
                "{nonsense:?}"
            );
        }
    }

    // ---- The four credential classes are disjoint by endpoint -------------

    #[test]
    fn the_data_plane_interceptor_admits_only_the_shared_token_and_robot_credentials() {
        // Data-plane half. `INGEST_PROVISIONING_TOKEN` and a
        // robot credential are different classes; only one of them may reach
        // `UploadService`, and neither may reach it *as* the operator.
        const OPERATOR: &str = "operator-token-that-is-long-enough-1234";
        const PROVISIONING: &str = "provisioning-token-that-is-long-enough-1";
        let credential = super::robot_auth::mint_robot_credential().expect("mint");
        let code =
            super::robot_auth::mint_enrollment_code("5NRHCGNQ4TR480B2FJMZQB1JYQ").expect("mint");

        assert_eq!(
            super::interceptor_decision(Some(OPERATOR), Some(&format!("Bearer {OPERATOR}"))),
            super::InterceptorDecision::Pass
        );
        assert_eq!(
            super::interceptor_decision(Some(OPERATOR), Some(&format!("Bearer {credential}"))),
            super::InterceptorDecision::DeferToHandler,
            "a shape-valid per-robot credential is resolved by the handler, not here"
        );
        // The provisioning token authorizes /admin/v2 and nothing else.
        assert_eq!(
            super::interceptor_decision(Some(OPERATOR), Some(&format!("Bearer {PROVISIONING}"))),
            super::InterceptorDecision::Reject(AuthOutcome::InvalidCredential)
        );
        // An enrollment *code* is not a bearer credential: `Enroll` reads it
        // from the message body, on a service outside this interceptor.
        assert_eq!(
            super::interceptor_decision(Some(OPERATOR), Some(&format!("Bearer {code}"))),
            super::InterceptorDecision::Reject(AuthOutcome::InvalidCredential)
        );
        assert_eq!(
            super::interceptor_decision(Some(OPERATOR), None),
            super::InterceptorDecision::Reject(AuthOutcome::MissingCredential)
        );
        // A near-miss `ignr1.` string is refused here, not deferred, so garbage
        // never costs a pool checkout.
        // `U` is not in Crockford's alphabet, so swapping the last character
        // for it keeps the length and breaks the charset.
        let bad_charset = format!("{}U", &credential[..credential.len() - 1]);
        for near_miss in [
            "ignr1.",
            "ignr1.TOOSHORT",
            &format!("{credential}0"),
            &bad_charset,
            &credential.replacen("ignr1.", "ign1.", 1),
        ] {
            assert_eq!(
                super::interceptor_decision(Some(OPERATOR), Some(near_miss)),
                super::InterceptorDecision::Reject(AuthOutcome::InvalidCredential),
                "{near_miss:?} must be refused before any database access"
            );
        }
    }

    #[test]
    fn the_operator_admin_route_refuses_every_other_credential_class() {
        // Admin-plane half: the `POST /admin/robots`
        // middleware is `evaluate_auth` against `INGEST_AUTH_TOKEN` alone, so a
        // robot credential and the provisioning token get the same body as
        // garbage.
        const OPERATOR: &str = "operator-token-that-is-long-enough-1234";
        const PROVISIONING: &str = "provisioning-token-that-is-long-enough-1";
        let credential = super::robot_auth::mint_robot_credential().expect("mint");
        for foreign in [PROVISIONING, credential.as_str(), "garbage"] {
            assert_eq!(
                evaluate_auth(Some(OPERATOR), Some(&format!("Bearer {foreign}"))),
                AuthOutcome::InvalidCredential,
                "{foreign} must not reach POST /admin/robots"
            );
        }
        assert_eq!(
            evaluate_auth(Some(OPERATOR), Some(&format!("Bearer {OPERATOR}"))),
            AuthOutcome::Allowed
        );
    }

    #[test]
    fn mtls_refuses_a_request_with_no_certificate_identity() {
        // A missing certificate identity
        // under `mtls` must be refused, NOT fall through to
        // `authorized_robot_filter(None, …)`'s fleet-wide behaviour.
        let request: tonic::Request<()> = tonic::Request::new(());
        let refused = super::extract_presented_credentials(&request, true)
            .expect_err("mtls requires a client certificate identity");
        assert_eq!(refused.code(), Code::Unauthenticated);
        assert!(
            refused.message().contains("missing_peer_certificate"),
            "{}",
            refused.message()
        );
        // ...and with the requirement off, the same request yields no identity,
        // which is the historical development behaviour.
        let presented = super::extract_presented_credentials(&request, false)
            .expect("dev/token do not require a certificate");
        assert!(presented.certificate_robot_id.is_none());
        assert!(presented.authorization.is_none());
    }

    // ---- /ca.crt is the QUERY service's route, not ingest's ----------------

    #[test]
    fn ingest_does_not_serve_the_ca_certificate() {
        // `/ca.crt` lives on query at 8003. Ingest's HTTP surface is 8080 and
        // stays unpublished, so a copy here could never be reached by the robot that
        // is its only caller — it would be dead code with a live-looking route.
        // This test is the standing guard against it growing back.
        let source = include_str!("main.rs");
        let route_registration = source
            .split("---- /ca.crt is the QUERY service's route")
            .next()
            .expect("the source before this test");
        assert!(
            !route_registration.contains("\"/ca.crt\""),
            "ingest must not register a /ca.crt route; it belongs to the query service on 8003"
        );
        // ...but ingest must still READ the same public CA file, because the
        // CA pin it mints has to agree with what the robot downloads. The pin
        // comes from `TLS_CA_CERT`, the CA of the ingest server certificate;
        // the old second name for that file must not come back.
        assert!(route_registration.contains("std::fs::read(&ca_cert_path)"));
        assert!(route_registration.contains("ca_pin_from_spki_digest"));
        let old_name = ["INGOTRON", "CA_CERT_FILE"].join("_");
        assert!(!route_registration.contains(&old_name));
    }

    #[test]
    fn the_enrollment_endpoint_uses_the_public_port() {
        use super::public_ingest_endpoint as endpoint;
        let host = Some("ingest.example.com");
        // The published port wins over the container's listen port.
        assert_eq!(
            endpoint(host, Some("443"), 50051).as_deref(),
            Ok("ingest.example.com:443")
        );
        assert_eq!(
            endpoint(host, Some(" 50052 "), 50051).as_deref(),
            Ok("ingest.example.com:50052")
        );
        // Unset or empty falls back to the listen port.
        assert_eq!(
            endpoint(host, None, 50051).as_deref(),
            Ok("ingest.example.com:50051")
        );
        assert_eq!(
            endpoint(host, Some(""), 50051).as_deref(),
            Ok("ingest.example.com:50051")
        );
        // An explicit host:port and a missing host keep their old meaning.
        assert_eq!(
            endpoint(Some("h:9"), Some("443"), 50051).as_deref(),
            Ok("h:9")
        );
        assert_eq!(
            endpoint(None, Some("443"), 50051).as_deref(),
            Ok("localhost:443")
        );
        // A bad port stops startup instead of minting unusable endpoints.
        for bad in ["0", "65536", "-1", "http", "50051x"] {
            assert!(endpoint(host, Some(bad), 50051).is_err(), "{bad}");
        }
    }

    #[test]
    fn the_bearer_scheme_is_stripped_the_same_way_everywhere() {
        // `resolve_request_identity` and the interceptor both parse the header;
        // if they disagreed, a `Bearer `-prefixed credential would be deferred
        // by one and rejected by the other.
        assert_eq!(strip_bearer_scheme("Bearer ignr1.AAA"), "ignr1.AAA");
        assert_eq!(strip_bearer_scheme("bearer  ignr1.AAA "), "ignr1.AAA");
        assert_eq!(strip_bearer_scheme("  ignr1.AAA  "), "ignr1.AAA");
        assert_eq!(strip_bearer_scheme("Basic ignr1.AAA"), "Basic ignr1.AAA");
    }

    // ---- Immutable client_upload_id reservation ----------------------------

    fn reserved_identity() -> ImmutableUploadIdentity {
        ImmutableUploadIdentity {
            client_upload_id: "upload-1".into(),
            robot_id: "rover-7".into(),
            filename: "capture.mcap".into(),
            file_size_bytes: 42,
            expected_sha256: SHA_A.into(),
        }
    }

    #[test]
    fn reservation_identity_accepts_only_an_exact_replay() {
        let original = reserved_identity();
        assert!(immutable_identity_conflicts(&original, &original).is_empty());

        let mut changed = original.clone();
        changed.robot_id = "rover-8".into();
        changed.filename = "other.mcap".into();
        changed.file_size_bytes += 1;
        changed.expected_sha256 = SHA_B.into();
        assert_eq!(
            immutable_identity_conflicts(&original, &changed),
            vec!["robot_id", "filename", "file_size", "expected_sha256"]
        );
    }

    #[test]
    fn reservation_digest_is_strict_and_normalized() {
        assert_eq!(normalize_sha256(&SHA_A.to_uppercase()).unwrap(), SHA_A);
        for invalid in ["", "abc", &"g".repeat(64), &format!("{}0", SHA_A)] {
            assert!(normalize_sha256(invalid).is_err(), "{invalid}");
        }
    }

    #[test]
    fn reservation_sql_persists_digest_before_object_io() {
        for column in [
            "client_upload_id",
            "robot_id",
            "original_filename",
            "file_size_bytes",
            "expected_sha256",
            "storage_path",
        ] {
            assert!(
                RESERVE_UPLOAD_INSERT_SQL.contains(column),
                "missing {column}"
            );
        }
        for guard in [
            "uploads.robot_id=$2",
            "uploads.original_filename=$3",
            "uploads.file_size_bytes=$5",
            "uploads.expected_sha256=$6",
        ] {
            assert!(
                COMPLETED_UPLOAD_UPSERT_SQL.contains(guard),
                "missing {guard}"
            );
        }
    }

    #[test]
    fn reserved_s3_path_is_authoritative_and_parseable() {
        assert_eq!(
            parse_s3_storage_path("s3://raw-vault/raw/mcap/r1/2026/08/09/u.mcap").unwrap(),
            (
                "raw-vault".to_string(),
                "raw/mcap/r1/2026/08/09/u.mcap".to_string()
            )
        );
        assert!(parse_s3_storage_path("https://example/object").is_err());
        assert!(parse_s3_storage_path("s3://bucket-only").is_err());
    }

    // ---- Per-upload CAS manifest registry -------------------------------

    #[test]
    fn manifest_registry_conflicts_on_upload_not_shared_digest() {
        assert!(
            MANIFEST_INSERT_SQL.contains("ON CONFLICT (upload_id) DO NOTHING"),
            "the upload owns its manifest registry row: {}",
            MANIFEST_INSERT_SQL
        );
        assert!(
            !MANIFEST_INSERT_SQL.contains("ON CONFLICT (file_sha256)"),
            "identical bytes from distinct uploads must each get a registry row: {}",
            MANIFEST_INSERT_SQL
        );
    }

    #[test]
    fn manifest_object_identity_and_body_are_per_upload() {
        assert_eq!(
            manifest_object_key("rover-7", "upload-1"),
            "manifests/rover-7/upload-1.json"
        );
        let document = cas_manifest_document(
            "upload-1",
            "rover-7",
            "capture.mcap",
            42,
            "application/octet-stream",
            SHA_A,
            &format!("cas/files/{}", SHA_A),
            &format!("s3://raw-vault/cas/files/{}", SHA_A),
        );
        let parsed: serde_json::Value =
            serde_json::from_str(&document).expect("manifest must be valid JSON");
        assert_eq!(parsed["upload_id"], "upload-1");
        assert_eq!(parsed["robot_id"], "rover-7");
        assert_eq!(parsed["file_sha256"], SHA_A);
        assert_eq!(parsed["size_bytes"], 42);
    }

    // ---- HealthCheck advertises the deployed per-robot limit -----------

    #[test]
    fn health_checks_advertise_the_configured_per_robot_limit() {
        // The Python client's `server_per_robot_stream_limit` reads this exact
        // key (see transport.py's `PER_ROBOT_LIMIT_CHECK_KEYS`) to learn the
        // server's per-robot stream limit instead of guessing a default.
        let checks = health_checks(7);
        assert_eq!(checks.get("per_robot_concurrency"), Some(&"7".to_string()));
    }

    #[test]
    fn built_in_healthcheck_requires_a_ready_http_response() {
        assert!(healthcheck_response_is_ready(b"HTTP/1.1 200 OK\r\n"));
        assert!(healthcheck_response_is_ready(b"HTTP/1.0 200 OK\r\n"));
        assert!(!healthcheck_response_is_ready(
            b"HTTP/1.1 503 Service Unavailable\r\n"
        ));
        assert!(!healthcheck_response_is_ready(b"not HTTP"));
    }

    #[test]
    fn built_in_healthcheck_reads_a_split_status_line_before_checking_it() {
        struct ChunkedReader {
            chunks: Vec<&'static [u8]>,
            chunk_index: usize,
            offset: usize,
        }

        impl std::io::Read for ChunkedReader {
            fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
                while self.chunk_index < self.chunks.len()
                    && self.offset == self.chunks[self.chunk_index].len()
                {
                    self.chunk_index += 1;
                    self.offset = 0;
                }
                if self.chunk_index == self.chunks.len() {
                    return Ok(0);
                }
                let chunk = self.chunks[self.chunk_index];
                let count = buffer.len().min(chunk.len() - self.offset);
                buffer[..count].copy_from_slice(&chunk[self.offset..self.offset + count]);
                self.offset += count;
                Ok(count)
            }
        }

        let mut response = ChunkedReader {
            chunks: vec![b"HTTP/1.", b"1 200 OK\r", b"\nbody is not read"],
            chunk_index: 0,
            offset: 0,
        };
        let status_line = read_healthcheck_status_line(&mut response).unwrap();

        assert_eq!(status_line, b"HTTP/1.1 200 OK\r\n");
        assert!(healthcheck_response_is_ready(&status_line));
    }
}
