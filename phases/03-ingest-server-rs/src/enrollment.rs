//! `RobotEnrollment.Enroll` — the one-use enrollment-code exchange.
//!
//! This service is deliberately *not* behind the shared-token interceptor: the
//! code is the credential. Everything that protects it is
//! in this file — a shape check before any pool checkout, a rate limiter, one
//! atomic transaction serialized on the `robots` row, and a single constant
//! failure shape for every rejection.
//!
//! **Constant-shape failures.** No-such-code, expired, revoked,
//! already-consumed, decommissioned robot and credential-cap-reached all return
//! byte-identical `UNAUTHENTICATED "invalid or expired enrollment code"`. Any
//! difference — including a difference the caller could only measure, like
//! "this one took a database round trip and that one did not" — tells an
//! attacker that a guessed code was real. The distinction is recorded
//! server-side, in `robot_lifecycle_events` and in
//! `ingest_enrollment_rejected_total{reason}`.

use std::net::IpAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use deadpool_postgres::Pool;
use metrics::counter;
use tonic::{Request, Response, Status};
use tracing::{info, warn};
use uuid::Uuid;

use crate::pb;
use crate::robot_auth::{self, CredentialCache, RobotRegistryCache, ROBOT_STATE_DECOMMISSIONED};

/// The single message every `Enroll` rejection carries.
pub(crate) const ENROLL_FAILURE_MESSAGE: &str = "invalid or expired enrollment code";

/// Code lifetime: default 15 minutes, operator-settable 60 s – 24 h, hard-capped at 24 h.
pub(crate) const DEFAULT_CODE_TTL_SECONDS: i64 = 900;
pub(crate) const MIN_CODE_TTL_SECONDS: i64 = 60;
pub(crate) const MAX_CODE_TTL_SECONDS: i64 = 24 * 60 * 60;
/// At most three unconsumed, unexpired codes per robot.
pub(crate) const MAX_OPEN_CODES_PER_ROBOT: i64 = 3;
/// A deliberate non-constraint in the schema: at most two live credentials per robot,
/// enforced in the issuing transaction under the `robots` row lock because the
/// liveness predicate involves `NOW()` and index predicates must be immutable.
pub(crate) const MAX_LIVE_CREDENTIALS_PER_ROBOT: i64 = 2;

pub(crate) const LIFECYCLE_EVENT_INSERT_SQL: &str = "\
INSERT INTO robot_lifecycle_events (robot_id, event, actor, detail) VALUES ($1, $2, $3, $4)";

/// The liveness term used by the two-credential cap. Deliberately the same
/// three predicates `robot_auth::classify_credential_row` applies, expressed in
/// SQL so the count is done by the database inside the locked transaction.
pub(crate) const LIVE_CREDENTIAL_COUNT_SQL: &str = "\
SELECT COUNT(*) FROM robot_credentials \
 WHERE robot_id = $1 \
   AND revoked_at IS NULL \
   AND (expires_at IS NULL OR expires_at > NOW()) \
   AND (superseded_at IS NULL OR superseded_grace_until > NOW())";

// ===== Rate limiting ========================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RateLimitReason {
    PerIp,
    Global,
    IpBlocked,
    /// The per-source table is at its hard cap and none of its entries can be
    /// evicted without losing a block, a failure run or an unspent refill.
    SourceTableFull,
}

impl RateLimitReason {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::PerIp => "rate_limited_ip",
            Self::Global => "rate_limited_global",
            Self::IpBlocked => "ip_blocked",
            Self::SourceTableFull => "rate_limited_source_table_full",
        }
    }
}

/// How often the per-source table is swept for idle entries.
const SWEEP_INTERVAL_SECONDS: u64 = 60;
/// Hard cap on tracked sources. One entry is under 100 bytes, so the cap
/// bounds the table at well under 1 MiB whatever the peer does.
const MAX_TRACKED_SOURCES: usize = 4096;

/// The limiter key for a peer address. IPv4-mapped IPv6 peers (seen on a
/// dual-stack listener) are keyed as plain IPv4. Other IPv6 peers are keyed by
/// their /64, because one host can rotate through a whole /64 at no cost.
fn source_key(ip: IpAddr) -> IpAddr {
    match ip.to_canonical() {
        IpAddr::V6(v6) => {
            let mut octets = v6.octets();
            octets[8..].fill(0);
            IpAddr::from(octets)
        }
        v4 => v4,
    }
}

/// A classic token bucket with an explicit clock, so the limiter's behaviour is
/// a unit test rather than a sleep.
#[derive(Debug, Clone)]
struct TokenBucket {
    tokens: f64,
    capacity: f64,
    refill_per_second: f64,
    last_refill: Instant,
}

impl TokenBucket {
    fn new(capacity: f64, refill_per_second: f64, now: Instant) -> Self {
        Self {
            tokens: capacity,
            capacity,
            refill_per_second,
            last_refill: now,
        }
    }

    fn try_take(&mut self, now: Instant) -> bool {
        let elapsed = now
            .saturating_duration_since(self.last_refill)
            .as_secs_f64();
        if elapsed > 0.0 {
            self.tokens = (self.tokens + elapsed * self.refill_per_second).min(self.capacity);
            self.last_refill = now;
        }
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    /// True when the bucket would be full at `now`, so dropping it and
    /// creating a fresh one later gives the source nothing extra.
    fn is_refilled(&self, now: Instant) -> bool {
        let elapsed = now
            .saturating_duration_since(self.last_refill)
            .as_secs_f64();
        self.tokens + elapsed * self.refill_per_second >= self.capacity
    }
}

#[derive(Debug, Clone)]
struct IpState {
    bucket: TokenBucket,
    consecutive_failures: u32,
    first_failure_at: Option<Instant>,
    blocked_until: Option<Instant>,
}

impl IpState {
    /// An entry can be evicted only when a fresh entry would behave exactly
    /// the same: no active block, no failure run inside the window, and a
    /// full bucket.
    fn is_idle(&self, now: Instant, failure_window: Duration) -> bool {
        self.blocked_until.is_none_or(|until| now >= until)
            && self
                .first_failure_at
                .is_none_or(|first| now.saturating_duration_since(first) > failure_window)
            && self.bucket.is_refilled(now)
    }
}

/// In-process limiter. Ingest is single-replica, so this needs no Redis.
/// A 160-bit code cannot be guessed — these bound
/// resource exhaustion and make a scan loud.
///
/// The per-source table is bounded. Idle entries are swept at most once per
/// `SWEEP_INTERVAL_SECONDS`, and a new source is refused when the table is at
/// `max_sources` and a forced sweep frees nothing. Entries are keyed by
/// `source_key`, so one IPv6 /64 is one source.
pub(crate) struct EnrollmentRateLimiter {
    per_ip: DashMap<IpAddr, IpState>,
    global: parking_lot::Mutex<TokenBucket>,
    per_ip_capacity: f64,
    per_ip_refill_per_second: f64,
    failure_threshold: u32,
    failure_window: Duration,
    block_duration: Duration,
    max_sources: usize,
    sweep_interval: Duration,
    last_sweep: parking_lot::Mutex<Instant>,
}

impl EnrollmentRateLimiter {
    /// The contract numbers: per-IP 5/min with burst 10, global 30/min, and a
    /// 15-minute block after 10 consecutive failures inside 5 minutes.
    pub(crate) fn with_contract_defaults(now: Instant) -> Self {
        Self::with_limits(now, 10.0, 5.0 / 60.0, 30.0, 30.0 / 60.0, 10)
    }

    pub(crate) fn with_limits(
        now: Instant,
        per_ip_capacity: f64,
        per_ip_refill_per_second: f64,
        global_capacity: f64,
        global_refill_per_second: f64,
        failure_threshold: u32,
    ) -> Self {
        Self {
            per_ip: DashMap::new(),
            global: parking_lot::Mutex::new(TokenBucket::new(
                global_capacity,
                global_refill_per_second,
                now,
            )),
            per_ip_capacity,
            per_ip_refill_per_second,
            failure_threshold,
            failure_window: Duration::from_secs(5 * 60),
            block_duration: Duration::from_secs(15 * 60),
            max_sources: MAX_TRACKED_SOURCES,
            sweep_interval: Duration::from_secs(SWEEP_INTERVAL_SECONDS),
            last_sweep: parking_lot::Mutex::new(now),
        }
    }

    #[cfg(test)]
    fn with_max_sources(mut self, max_sources: usize) -> Self {
        self.max_sources = max_sources;
        self
    }

    #[cfg(test)]
    fn tracked_sources(&self) -> usize {
        self.per_ip.len()
    }

    /// Remove every idle entry. The caller must not hold a map guard:
    /// `retain` locks each shard in turn.
    fn sweep(&self, now: Instant) {
        *self.last_sweep.lock() = now;
        let window = self.failure_window;
        self.per_ip.retain(|_, state| !state.is_idle(now, window));
    }

    /// Sweep when the interval has passed, and make room for `key` if it is
    /// new and the table is full. Returns false when no room can be made.
    fn admit_source(&self, key: IpAddr, now: Instant) -> bool {
        let due = now.saturating_duration_since(*self.last_sweep.lock()) >= self.sweep_interval;
        if due {
            self.sweep(now);
        }
        if self.per_ip.len() < self.max_sources || self.per_ip.contains_key(&key) {
            return true;
        }
        if !due {
            self.sweep(now);
        }
        // Concurrent first requests can overshoot the cap by at most the
        // number of requests in flight; the cap still bounds the table.
        self.per_ip.len() < self.max_sources
    }

    /// Check the per-IP block, then the per-IP bucket, then the global bucket.
    ///
    /// Order matters: a blocked IP must not be able to drain the global bucket
    /// and take the whole deployment's enrollment capacity with it.
    pub(crate) fn check(&self, ip: IpAddr, now: Instant) -> Result<(), RateLimitReason> {
        let key = source_key(ip);
        if !self.admit_source(key, now) {
            return Err(RateLimitReason::SourceTableFull);
        }
        {
            let mut entry = self.per_ip.entry(key).or_insert_with(|| IpState {
                bucket: TokenBucket::new(self.per_ip_capacity, self.per_ip_refill_per_second, now),
                consecutive_failures: 0,
                first_failure_at: None,
                blocked_until: None,
            });
            if let Some(until) = entry.blocked_until {
                if now < until {
                    return Err(RateLimitReason::IpBlocked);
                }
                entry.blocked_until = None;
                entry.consecutive_failures = 0;
                entry.first_failure_at = None;
            }
            if !entry.bucket.try_take(now) {
                return Err(RateLimitReason::PerIp);
            }
        }
        if !self.global.lock().try_take(now) {
            return Err(RateLimitReason::Global);
        }
        Ok(())
    }

    pub(crate) fn record_failure(&self, ip: IpAddr, now: Instant) {
        let Some(mut entry) = self.per_ip.get_mut(&source_key(ip)) else {
            return;
        };
        match entry.first_failure_at {
            Some(first) if now.saturating_duration_since(first) <= self.failure_window => {
                entry.consecutive_failures += 1;
            }
            _ => {
                entry.first_failure_at = Some(now);
                entry.consecutive_failures = 1;
            }
        }
        if entry.consecutive_failures >= self.failure_threshold {
            entry.blocked_until = Some(now + self.block_duration);
        }
    }

    /// A success clears the consecutive-failure run: the block is for a scan,
    /// not for a technician who fat-fingered a code and then got it right.
    pub(crate) fn record_success(&self, ip: IpAddr) {
        if let Some(mut entry) = self.per_ip.get_mut(&source_key(ip)) {
            entry.consecutive_failures = 0;
            entry.first_failure_at = None;
        }
    }
}

// ===== The service ==========================================================

/// Server-recommended agent settings returned with a successful enrollment.
#[derive(Debug, Clone, Copy)]
pub(crate) struct RecommendedUploadSettings {
    pub(crate) chunk_size_bytes: i64,
    pub(crate) parallel_streams: i32,
    pub(crate) min_stable_age_seconds: f64,
}

/// Everything `Enroll` needs, assembled by `main`. Kept separate from
/// `AppState` so the enrollment surface cannot accidentally reach S3, the
/// upload semaphores, or the shared operator token.
#[derive(Clone)]
pub(crate) struct EnrollmentService {
    pub(crate) pool: Pool,
    pub(crate) credential_cache: CredentialCache,
    pub(crate) registry_cache: RobotRegistryCache,
    pub(crate) rate_limiter: Arc<EnrollmentRateLimiter>,
    /// From `INGEST_PUBLIC_HOST`; must match the server certificate the robot
    /// will verify.
    pub(crate) ingest_endpoint: String,
    pub(crate) recommended: RecommendedUploadSettings,
    pub(crate) self_rotation_enabled: bool,
}

/// Why an exchange failed, for the metric label and the audit row only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EnrollRejection {
    Malformed,
    UnknownCode,
    AlreadyConsumed,
    Revoked,
    Expired,
    RobotDecommissioned,
    CredentialCapReached,
    RaceLost,
}

impl EnrollRejection {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Malformed => "malformed_code",
            Self::UnknownCode => "unknown_code",
            Self::AlreadyConsumed => "already_consumed",
            Self::Revoked => "revoked",
            Self::Expired => "expired",
            Self::RobotDecommissioned => "robot_decommissioned",
            Self::CredentialCapReached => "credential_cap_reached",
            Self::RaceLost => "race_lost",
        }
    }
}

/// Classify a code row that the consuming `UPDATE` would refuse. Pure, so every
/// branch is a unit test rather than a database fixture.
pub(crate) fn classify_code_row(
    consumed_at: Option<chrono::DateTime<chrono::Utc>>,
    revoked_at: Option<chrono::DateTime<chrono::Utc>>,
    expires_at: chrono::DateTime<chrono::Utc>,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<(), EnrollRejection> {
    if consumed_at.is_some() {
        return Err(EnrollRejection::AlreadyConsumed);
    }
    if revoked_at.is_some() {
        return Err(EnrollRejection::Revoked);
    }
    if expires_at <= now {
        return Err(EnrollRejection::Expired);
    }
    Ok(())
}

/// Clamp an operator-supplied TTL into the code-lifetime window. `None` and every
/// out-of-range value resolve to something inside the contract rather than
/// failing the mint, except that a caller asking for more than the hard cap is
/// given the cap, never the request.
pub(crate) fn resolve_code_ttl_seconds(requested: Option<i64>) -> i64 {
    match requested {
        None => DEFAULT_CODE_TTL_SECONDS,
        Some(value) => value.clamp(MIN_CODE_TTL_SECONDS, MAX_CODE_TTL_SECONDS),
    }
}

fn enroll_failure(rejection: EnrollRejection) -> Status {
    counter!("ingest_enrollment_rejected_total", "reason" => rejection.label()).increment(1);
    Status::unauthenticated(ENROLL_FAILURE_MESSAGE)
}

fn rate_limit_failure(reason: RateLimitReason) -> Status {
    counter!("ingest_enrollment_rejected_total", "reason" => reason.label()).increment(1);
    Status::resource_exhausted("enrollment rate limit exceeded")
}

#[tonic::async_trait]
impl pb::robot_enrollment_server::RobotEnrollment for EnrollmentService {
    async fn enroll(
        &self,
        request: Request<pb::EnrollRequest>,
    ) -> Result<Response<pb::CredentialResponse>, Status> {
        let now = Instant::now();
        let peer_ip = request
            .remote_addr()
            .map(|address| address.ip())
            .unwrap_or(IpAddr::from([0, 0, 0, 0]));
        if let Err(reason) = self.rate_limiter.check(peer_ip, now) {
            return Err(rate_limit_failure(reason));
        }
        let message = request.into_inner();

        // Shape first: a malformed code never reaches the pool.
        let parsed = match robot_auth::parse_enrollment_code(&message.code) {
            Ok(parsed) => parsed,
            Err(format_error) => {
                counter!(
                    "ingest_enrollment_rejected_total",
                    "reason" => "malformed_code",
                    "detail" => format_error.label()
                )
                .increment(1);
                self.rate_limiter.record_failure(peer_ip, now);
                return Err(enroll_failure(EnrollRejection::Malformed));
            }
        };

        match self.exchange(&parsed, peer_ip, &message).await {
            Ok(response) => {
                self.rate_limiter.record_success(peer_ip);
                Ok(Response::new(response))
            }
            Err(ExchangeError::Rejected(rejection)) => {
                self.rate_limiter.record_failure(peer_ip, now);
                Err(enroll_failure(rejection))
            }
            Err(ExchangeError::Unavailable(detail)) => {
                // Not a credential failure: say so, so an operator debugging a
                // dead database is not told the technician's code is bad.
                warn!(%detail, "enrollment exchange unavailable");
                Err(Status::unavailable(
                    "enrollment service temporarily unavailable",
                ))
            }
        }
    }
}

pub(crate) enum ExchangeError {
    Rejected(EnrollRejection),
    Unavailable(String),
}

impl EnrollmentService {
    /// The atomic code-for-credential exchange.
    ///
    /// **Statement order, and why.** A naive order runs the consuming
    /// `UPDATE … SET consumed_at = NOW()` first and fills
    /// `consumed_credential_id` in a later statement. The schema carries
    /// `robot_enrollment_codes_consumed_pair
    /// CHECK ((consumed_at IS NULL) = (consumed_credential_id IS NULL))`, and a
    /// PostgreSQL `CHECK` is evaluated per row per statement and cannot be
    /// deferred — so that first statement would violate the constraint every
    /// time. The credential is therefore inserted first, with an id generated
    /// here, and the consume sets both columns in the *same* statement.
    ///
    /// Every property of the exchange holds: the one-use guarantee is still
    /// `AND consumed_at IS NULL` inside one `UPDATE … RETURNING` under the
    /// `robots` row lock; consume and issue still commit together; and a crash
    /// mid-exchange still leaves the code unconsumed and retryable, because the
    /// credential insert rolls back with it.
    async fn exchange(
        &self,
        parsed: &robot_auth::ParsedEnrollmentCode,
        peer_ip: IpAddr,
        message: &pb::EnrollRequest,
    ) -> Result<pb::CredentialResponse, ExchangeError> {
        let code_hash = parsed.code_sha256.to_vec();
        let mut client = self
            .pool
            .get()
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("pool: {error}")))?;
        let transaction = client
            .transaction()
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("begin: {error}")))?;

        // 1. Lock the robots row the code points at. This is the serialization
        //    point for everything touching that robot's credentials.
        let robot_row = transaction
            .query_opt(
                "SELECT id, state, auth_mode FROM robots WHERE id = ( \
                     SELECT robot_id FROM robot_enrollment_codes WHERE code_sha256 = $1 \
                 ) FOR UPDATE",
                &[&code_hash],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("lock: {error}")))?;
        let Some(robot_row) = robot_row else {
            // No code, or a code whose robot vanished. Nothing to audit against.
            return Err(ExchangeError::Rejected(EnrollRejection::UnknownCode));
        };
        let robot_id: String = robot_row.get(0);
        let robot_state: String = robot_row.get(1);

        // 2. Read the code row under that lock and classify it. A rejection is
        //    audited and COMMITTED, because an audit row for a scan is the
        //    whole point of making a scan loud.
        let code_row = transaction
            .query_opt(
                "SELECT code_id, purpose, consumed_at, revoked_at, expires_at \
                   FROM robot_enrollment_codes WHERE code_sha256 = $1",
                &[&code_hash],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("code lookup: {error}")))?;
        let Some(code_row) = code_row else {
            return Err(ExchangeError::Rejected(EnrollRejection::UnknownCode));
        };
        let code_id: Uuid = code_row.get(0);
        let purpose: String = code_row.get(1);
        let rejection = classify_code_row(
            code_row.get(2),
            code_row.get(3),
            code_row.get(4),
            chrono::Utc::now(),
        )
        .err()
        .or_else(|| {
            (robot_state == ROBOT_STATE_DECOMMISSIONED)
                .then_some(EnrollRejection::RobotDecommissioned)
        });
        if let Some(rejection) = rejection {
            self.audit_rejection(
                &transaction,
                &robot_id,
                &parsed.fingerprint(),
                rejection,
                peer_ip,
            )
            .await?;
            transaction
                .commit()
                .await
                .map_err(|error| ExchangeError::Unavailable(format!("audit commit: {error}")))?;
            return Err(ExchangeError::Rejected(rejection));
        }

        // 3. The two-live-credential cap, enforced here rather than by an index.
        let live: i64 = transaction
            .query_one(LIVE_CREDENTIAL_COUNT_SQL, &[&robot_id])
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("credential count: {error}")))?
            .get(0);
        if live >= MAX_LIVE_CREDENTIALS_PER_ROBOT {
            self.audit_rejection(
                &transaction,
                &robot_id,
                &parsed.fingerprint(),
                EnrollRejection::CredentialCapReached,
                peer_ip,
            )
            .await?;
            transaction
                .commit()
                .await
                .map_err(|error| ExchangeError::Unavailable(format!("audit commit: {error}")))?;
            return Err(ExchangeError::Rejected(
                EnrollRejection::CredentialCapReached,
            ));
        }

        // 4. Mint and insert the credential, then consume the code with the
        //    credential id in the same statement (see the doc comment above).
        let credential = robot_auth::mint_robot_credential()
            .map_err(|error| ExchangeError::Unavailable(format!("csprng: {error}")))?;
        let token_sha256 = robot_auth::sha256_of(&credential);
        let credential_fingerprint = robot_auth::fingerprint_of(&token_sha256);
        let credential_id = Uuid::new_v4();
        transaction
            .execute(
                "INSERT INTO robot_credentials \
                     (credential_id, robot_id, token_sha256, fingerprint, issued_via) \
                 VALUES ($1, $2, $3, $4, 'enrollment')",
                &[
                    &credential_id,
                    &robot_id,
                    &token_sha256.as_slice(),
                    &credential_fingerprint,
                ],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("credential insert: {error}")))?;

        let consumed = transaction
            .query_opt(
                "UPDATE robot_enrollment_codes \
                    SET consumed_at = NOW(), consumed_from_ip = $2, consumed_credential_id = $3 \
                  WHERE code_sha256 = $1 \
                    AND consumed_at IS NULL \
                    AND revoked_at IS NULL \
                    AND expires_at > NOW() \
                RETURNING code_id, robot_id, purpose",
                &[&code_hash, &peer_ip, &credential_id],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("consume: {error}")))?;
        if consumed.is_none() {
            // Zero rows: a concurrent redemption won, or the code expired
            // between step 2 and here. Roll the credential back with it.
            return Err(ExchangeError::Rejected(EnrollRejection::RaceLost));
        }

        // 5. Flip the registry row. `COALESCE(enrolled_at, NOW())` is what makes
        //    two concurrent redemptions of two different codes end
        //    without a lost update.
        transaction
            .execute(
                "UPDATE robots \
                    SET state = 'enrolled', auth_mode = 'per_robot', \
                        enrolled_at = COALESCE(enrolled_at, NOW()), \
                        state_changed_at = NOW(), updated_at = NOW() \
                  WHERE id = $1",
                &[&robot_id],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("robot state: {error}")))?;

        let detail = serde_json::json!({
            "code_id": code_id.to_string(),
            "code_fingerprint": parsed.fingerprint(),
            "purpose": purpose,
            "credential_id": credential_id.to_string(),
            "credential_fingerprint": credential_fingerprint,
            "consumed_from_ip": peer_ip.to_string(),
            "agent_version": sanitize_display(&message.agent_version),
            "hostname": sanitize_display(&message.hostname),
            "platform": sanitize_display(&message.platform),
        });
        for event in ["code_redeemed", "enrolled"] {
            transaction
                .execute(
                    LIFECYCLE_EVENT_INSERT_SQL,
                    &[&robot_id, &event, &Option::<String>::None, &detail],
                )
                .await
                .map_err(|error| ExchangeError::Unavailable(format!("audit: {error}")))?;
        }
        transaction
            .commit()
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("commit: {error}")))?;

        // Ingest owns every lifecycle write, so it invalidates its own caches
        // synchronously on the commit path.
        self.registry_cache.invalidate(&robot_id);
        self.credential_cache.invalidate_robot(&robot_id);
        info!(
            robot_id = %robot_id,
            credential_fingerprint = %credential_fingerprint,
            code_fingerprint = %parsed.fingerprint(),
            "robot enrolled"
        );
        counter!("ingest_enrollment_completed_total").increment(1);

        Ok(pb::CredentialResponse {
            robot_id,
            credential,
            credential_fingerprint,
            ingest_endpoint: self.ingest_endpoint.clone(),
            recommended: Some(pb::UploadSettings {
                chunk_size_bytes: self.recommended.chunk_size_bytes,
                parallel_streams: self.recommended.parallel_streams,
                min_stable_age_seconds: self.recommended.min_stable_age_seconds,
            }),
            supersedes_grace_seconds: 0,
            self_rotation_enabled: self.self_rotation_enabled,
        })
    }

    async fn audit_rejection(
        &self,
        transaction: &deadpool_postgres::Transaction<'_>,
        robot_id: &str,
        code_fingerprint: &str,
        rejection: EnrollRejection,
        peer_ip: IpAddr,
    ) -> Result<(), ExchangeError> {
        let detail = serde_json::json!({
            "reason": rejection.label(),
            "code_fingerprint": code_fingerprint,
            "from_ip": peer_ip.to_string(),
        });
        transaction
            .execute(
                LIFECYCLE_EVENT_INSERT_SQL,
                &[
                    &robot_id,
                    &"code_rejected",
                    &Option::<String>::None,
                    &detail,
                ],
            )
            .await
            .map_err(|error| ExchangeError::Unavailable(format!("audit: {error}")))?;
        Ok(())
    }
}

/// `X-Ingotron-Actor`, `hostname`, `agent_version` and `platform` are all
/// asserted by someone else and stored for display. Strip control characters
/// (including the newline that would forge a log line) and truncate.
pub(crate) fn sanitize_display(value: &str) -> String {
    value
        .chars()
        .filter(|character| !character.is_control())
        .take(255)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration as ChronoDuration, Utc};

    fn ip(last: u8) -> IpAddr {
        IpAddr::from([10, 0, 0, last])
    }

    // ---- Rate limits ---------------------------------------------------------

    #[test]
    fn the_eleventh_attempt_from_one_ip_within_a_minute_is_refused() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for attempt in 1..=10 {
            assert_eq!(
                limiter.check(ip(1), start),
                Ok(()),
                "attempt {attempt} is inside the burst of 10"
            );
        }
        assert_eq!(limiter.check(ip(1), start), Err(RateLimitReason::PerIp));
        // "a different IP is unaffected"
        assert_eq!(limiter.check(ip(2), start), Ok(()));
    }

    #[test]
    fn the_per_ip_bucket_refills_at_five_a_minute() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for _ in 0..10 {
            limiter.check(ip(1), start).expect("burst");
        }
        assert!(limiter.check(ip(1), start).is_err());
        // 11 s buys just under one token; 13 s buys one.
        assert!(limiter
            .check(ip(1), start + Duration::from_secs(11))
            .is_err());
        assert_eq!(
            limiter.check(ip(1), start + Duration::from_secs(13)),
            Ok(())
        );
    }

    #[test]
    fn the_global_bucket_bounds_a_distributed_scan() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        // 30 global tokens, spread across IPs that each stay under their own
        // limit, so only the global bucket can stop this.
        let mut allowed = 0;
        for host in 0..10u8 {
            for _ in 0..5 {
                if limiter.check(ip(host), start).is_ok() {
                    allowed += 1;
                }
            }
        }
        assert_eq!(
            allowed, 30,
            "the global bucket caps the deployment at 30/min"
        );
        assert_eq!(limiter.check(ip(99), start), Err(RateLimitReason::Global));
    }

    #[test]
    fn ten_consecutive_failures_block_an_ip_for_fifteen_minutes() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for _ in 0..10 {
            limiter.check(ip(1), start).expect("inside the burst");
            limiter.record_failure(ip(1), start);
        }
        assert_eq!(
            limiter.check(ip(1), start + Duration::from_secs(60 * 14)),
            Err(RateLimitReason::IpBlocked)
        );
        // The block outlives the token bucket's refill, which is the point:
        // waiting out the bucket must not wait out the block.
        assert_eq!(
            limiter.check(ip(1), start + Duration::from_secs(60 * 16)),
            Ok(())
        );
    }

    #[test]
    fn a_success_clears_the_failure_run() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for _ in 0..9 {
            limiter.check(ip(1), start).expect("inside the burst");
            limiter.record_failure(ip(1), start);
        }
        limiter.record_success(ip(1));
        limiter.check(ip(1), start).expect("inside the burst");
        limiter.record_failure(ip(1), start);
        assert_eq!(
            limiter.check(ip(1), start + Duration::from_secs(60 * 5)),
            Ok(()),
            "one failure after a success must not trip a 10-failure block"
        );
    }

    // ---- bounded per-source state -----------------------------------------
    //
    // Ways the eviction could fail, written down before the code:
    //  1. Rotating source addresses grow the map without bound (the defect).
    //  2. Eviction drops a blocked or failing source, so an attacker escapes
    //     the 15-minute block by flooding the map with other addresses.
    //  3. Eviction drops a source whose bucket is not yet full, so the source
    //     gets a fresh burst of 10 it should not have.
    //  4. One IPv6 host rotates through its /64 and gets a new bucket for each
    //     address.
    //  5. IPv4 peers that arrive as IPv4-mapped IPv6 (`::ffff:a.b.c.d`) on a
    //     dual-stack listener are masked to one shared /64 and lock each
    //     other out.
    //  6. At the hard cap, a new source is admitted anyway (cap not enforced)
    //     or a known source is refused (cap applied to the wrong requests).
    //  7. A full sweep runs on every request and costs O(n) per call.
    //  8. The sweep runs while a map entry guard is held and deadlocks.

    fn v6(text: &str) -> IpAddr {
        text.parse().expect("test address")
    }

    #[test]
    fn idle_sources_are_swept_after_the_sweep_interval() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for host in 0..20u8 {
            limiter.check(ip(host), start).expect("first attempt");
        }
        assert_eq!(limiter.tracked_sources(), 20);
        // Inside the sweep interval nothing is swept, even though no bucket
        // is being touched (failure 7: no sweep on every call).
        let soon = start + Duration::from_secs(30);
        for _ in 0..10 {
            limiter.check(ip(200), soon).expect("inside the burst");
        }
        assert_eq!(limiter.tracked_sources(), 21);
        // After the interval the 20 idle, refilled sources go; the one that
        // spent its burst 31 s earlier has not refilled and stays (failure 3).
        let later = start + Duration::from_secs(SWEEP_INTERVAL_SECONDS + 1);
        limiter.check(ip(201), later).expect("new source");
        assert_eq!(limiter.tracked_sources(), 2);
    }

    #[test]
    fn a_blocked_source_survives_sweeps_and_cap_pressure() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start).with_max_sources(4);
        for _ in 0..10 {
            limiter.check(ip(1), start).expect("inside the burst");
            limiter.record_failure(ip(1), start);
        }
        // Fill the rest of the cap, then try to push the blocked source out
        // with fresh addresses long after every bucket has refilled.
        for host in 2..=4u8 {
            limiter.check(ip(host), start).expect("below the cap");
        }
        let later = start + Duration::from_secs(10 * 60);
        for host in 10..40u8 {
            let _ = limiter.check(ip(host), later);
        }
        assert!(limiter.tracked_sources() <= 4, "the hard cap holds");
        assert_eq!(
            limiter.check(ip(1), later),
            Err(RateLimitReason::IpBlocked),
            "eviction must not lift a block (failure 2)"
        );
    }

    #[test]
    fn a_full_table_of_active_sources_refuses_only_new_sources() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start).with_max_sources(3);
        for host in 1..=3u8 {
            limiter.check(ip(host), start).expect("below the cap");
        }
        assert_eq!(
            limiter.check(ip(4), start),
            Err(RateLimitReason::SourceTableFull)
        );
        assert_eq!(limiter.tracked_sources(), 3);
        // A source already in the table is still served (failure 6).
        assert_eq!(limiter.check(ip(1), start), Ok(()));
        // Once the table's sources have refilled, a new source can enter.
        let later = start + Duration::from_secs(5 * 60);
        assert_eq!(limiter.check(ip(4), later), Ok(()));
    }

    #[test]
    fn one_ipv6_slash_64_shares_one_bucket() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for host in 1..=10u16 {
            let address = v6(&format!("2001:db8:1:2::{host:x}"));
            limiter.check(address, start).expect("inside the burst");
        }
        assert_eq!(
            limiter.check(v6("2001:db8:1:2:ffff::1"), start),
            Err(RateLimitReason::PerIp),
            "rotating inside one /64 must not buy a new bucket (failure 4)"
        );
        assert_eq!(limiter.check(v6("2001:db8:1:3::1"), start), Ok(()));
        assert_eq!(limiter.tracked_sources(), 2);
        // Failures recorded against any address in the /64 count together.
        for host in 0..10u16 {
            limiter.record_failure(v6(&format!("2001:db8:1:2::{:x}", 100 + host)), start);
        }
        assert_eq!(
            limiter.check(v6("2001:db8:1:2::abcd"), start + Duration::from_secs(60)),
            Err(RateLimitReason::IpBlocked)
        );
    }

    #[test]
    fn ipv4_mapped_peers_are_keyed_as_ipv4() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for _ in 0..10 {
            limiter
                .check(v6("::ffff:10.0.0.1"), start)
                .expect("inside the burst");
        }
        assert_eq!(limiter.check(ip(1), start), Err(RateLimitReason::PerIp));
        assert_eq!(
            limiter.check(v6("::ffff:10.0.0.2"), start),
            Ok(()),
            "a different IPv4 peer must not share the mapped /64 (failure 5)"
        );
    }

    #[test]
    fn failures_outside_the_five_minute_window_do_not_accumulate() {
        let start = Instant::now();
        let limiter = EnrollmentRateLimiter::with_contract_defaults(start);
        for step in 0..9 {
            let now = start + Duration::from_secs(step * 60);
            let _ = limiter.check(ip(1), now);
            limiter.record_failure(ip(1), now);
        }
        let later = start + Duration::from_secs(9 * 60);
        assert_eq!(limiter.check(ip(1), later), Ok(()));
    }

    // ---- code-row classification -------------------------------------------

    #[test]
    fn every_code_rejection_is_classified_and_none_reaches_the_caller() {
        let now = Utc::now();
        let past = now - ChronoDuration::seconds(1);
        let future = now + ChronoDuration::minutes(15);
        assert_eq!(classify_code_row(None, None, future, now), Ok(()));
        assert_eq!(
            classify_code_row(Some(past), None, future, now),
            Err(EnrollRejection::AlreadyConsumed)
        );
        assert_eq!(
            classify_code_row(None, Some(past), future, now),
            Err(EnrollRejection::Revoked)
        );
        assert_eq!(
            classify_code_row(None, None, past, now),
            Err(EnrollRejection::Expired)
        );
        // Exactly at expiry the code is dead: `expires_at > NOW()` is the
        // consuming UPDATE's predicate, so the classifier must agree.
        assert_eq!(
            classify_code_row(None, None, now, now),
            Err(EnrollRejection::Expired)
        );
        // Consumption is reported ahead of revocation and expiry: an operator
        // reading the audit trail needs to know the code was *used* (and so
        // may have leaked), not merely that it is no longer valid.
        assert_eq!(
            classify_code_row(Some(past), Some(past), past, now),
            Err(EnrollRejection::AlreadyConsumed)
        );
    }

    #[test]
    fn every_rejection_maps_to_one_status_and_one_message() {
        // The failure shape must not vary by cause.
        let shapes: Vec<(tonic::Code, String)> = [
            EnrollRejection::Malformed,
            EnrollRejection::UnknownCode,
            EnrollRejection::AlreadyConsumed,
            EnrollRejection::Revoked,
            EnrollRejection::Expired,
            EnrollRejection::RobotDecommissioned,
            EnrollRejection::CredentialCapReached,
            EnrollRejection::RaceLost,
        ]
        .into_iter()
        .map(|rejection| {
            let status = enroll_failure(rejection);
            (status.code(), status.message().to_string())
        })
        .collect();
        let first = shapes[0].clone();
        assert_eq!(first.0, tonic::Code::Unauthenticated);
        assert_eq!(first.1, ENROLL_FAILURE_MESSAGE);
        for shape in &shapes {
            assert_eq!(
                *shape, first,
                "every Enroll rejection must be byte-identical"
            );
        }
        // ...and every one of them must have a distinct audit label, so the
        // constant shape costs the operator nothing.
        let labels: std::collections::BTreeSet<&str> = [
            EnrollRejection::Malformed,
            EnrollRejection::UnknownCode,
            EnrollRejection::AlreadyConsumed,
            EnrollRejection::Revoked,
            EnrollRejection::Expired,
            EnrollRejection::RobotDecommissioned,
            EnrollRejection::CredentialCapReached,
            EnrollRejection::RaceLost,
        ]
        .into_iter()
        .map(EnrollRejection::label)
        .collect();
        assert_eq!(labels.len(), 8);
    }

    #[test]
    fn the_consuming_update_carries_all_three_one_use_predicates() {
        // A source assertion, deliberately, and here is the reason: the
        // pre-flight classification a few lines earlier runs under the same
        // `robots` row lock, so it *already* rejects a consumed, revoked or
        // expired code. The predicates on the `UPDATE … RETURNING` are
        // therefore redundant by construction — and that redundancy is exactly
        // what makes the exchange one-use. Behavioural tests cannot
        // distinguish "both guards" from "only the pre-flight", so a mutation
        // that deletes the second one survives every black-box test in this
        // suite. This is the test that kills it.
        let source = include_str!("enrollment.rs");
        let start = source
            .find("UPDATE robot_enrollment_codes \\")
            .expect("the consuming statement");
        let consume = &source[start..start + 400];
        for predicate in [
            "AND consumed_at IS NULL",
            "AND revoked_at IS NULL",
            "AND expires_at > NOW()",
            "RETURNING code_id, robot_id, purpose",
        ] {
            assert!(
                consume.contains(predicate),
                "the consuming UPDATE must keep `{predicate}`; it is the one-use guarantee, \
                 not a duplicate of the pre-flight check"
            );
        }
        // ...and it must set both halves of `robot_enrollment_codes_consumed_pair`
        // in that same statement, because a PostgreSQL CHECK cannot be deferred.
        assert!(consume.contains("consumed_credential_id = $3"));
    }

    // ---- TTL clamp ---------------------------------------------------------

    #[test]
    fn the_code_ttl_is_clamped_into_the_contract_window() {
        assert_eq!(resolve_code_ttl_seconds(None), DEFAULT_CODE_TTL_SECONDS);
        assert_eq!(resolve_code_ttl_seconds(Some(900)), 900);
        assert_eq!(resolve_code_ttl_seconds(Some(1)), MIN_CODE_TTL_SECONDS);
        assert_eq!(resolve_code_ttl_seconds(Some(0)), MIN_CODE_TTL_SECONDS);
        assert_eq!(resolve_code_ttl_seconds(Some(-9)), MIN_CODE_TTL_SECONDS);
        assert_eq!(
            resolve_code_ttl_seconds(Some(i64::MAX)),
            MAX_CODE_TTL_SECONDS,
            "the 24 h cap is hard, never advisory"
        );
    }

    #[test]
    fn untrusted_display_strings_cannot_forge_a_log_line() {
        assert_eq!(sanitize_display("rover-7"), "rover-7");
        assert_eq!(
            sanitize_display("ok\nlevel=INFO forged=true"),
            "oklevel=INFO forged=true"
        );
        assert_eq!(sanitize_display("a\u{0}b\r\tc"), "abc");
        assert_eq!(sanitize_display(&"x".repeat(1000)).len(), 255);
    }
}
