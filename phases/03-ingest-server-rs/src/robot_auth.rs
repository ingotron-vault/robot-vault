//! Per-robot credential and enrollment-code formats, their verification, and
//! the two TTL caches that keep verification off the hot path.
//!
//! Three things live here rather than in `main.rs` because they are the pieces
//! a reviewer has to be able to read end to end:
//!
//! 1. **The formats and their parsers.** Both are rejected on shape *before*
//!    any database access, so an unauthenticated caller cannot spend a pool
//!    connection with a malformed string.
//! 2. **`AuthenticatedRobot`.** One type for both identity sources, so the five
//!    `UploadService` ownership call sites constrain a bearer-authenticated
//!    robot exactly as they already constrain an mTLS one.
//! 3. **The verification predicates.** `expires_at` and the supersede grace are
//!    checked independently, against the columns the schema actually
//!    has.

use std::sync::Arc;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use deadpool_postgres::Pool;
use sha2::{Digest, Sha256};
use uuid::Uuid;
use x509_parser::parse_x509_certificate;
use x509_parser::pem::parse_x509_pem;

use crate::crockford;

// ===== Wire formats =========================================================

/// Version tag of the enrollment code. A code not starting with this is
/// rejected before any database access.
pub(crate) const ENROLLMENT_CODE_PREFIX: &str = "ign1.";
/// Version tag of a per-robot bearer credential.
pub(crate) const ROBOT_CREDENTIAL_PREFIX: &str = "ignr1.";
/// 20 random bytes = 160 bits = exactly 32 unpadded Crockford characters.
pub(crate) const ENROLLMENT_SECRET_BYTES: usize = 20;
pub(crate) const ENROLLMENT_SECRET_CHARS: usize = 32;
/// 32 random bytes = 256 bits = 52 unpadded Crockford characters.
pub(crate) const ROBOT_CREDENTIAL_SECRET_BYTES: usize = 32;
pub(crate) const ROBOT_CREDENTIAL_SECRET_CHARS: usize = 52;
/// `ign1.` + 26 + `.` + 32.
pub(crate) const ENROLLMENT_CODE_CHARS: usize = 64;
/// `ignr1.` + 52.
pub(crate) const ROBOT_CREDENTIAL_CHARS: usize = 58;

/// Why a presented enrollment code was not even worth a database round trip.
///
/// The *caller* never sees these: every rejection has one constant failure
/// shape. They exist for the audit row and the metric label.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CodeFormatError {
    Empty,
    WrongVersion,
    WrongLength,
    WrongShape,
    NonCrockford,
}

impl CodeFormatError {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Empty => "empty",
            Self::WrongVersion => "wrong_version",
            Self::WrongLength => "wrong_length",
            Self::WrongShape => "wrong_shape",
            Self::NonCrockford => "non_crockford",
        }
    }
}

/// A syntactically valid `ign1.<pin>.<secret>`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ParsedEnrollmentCode {
    /// The 26-character CA pin segment, exactly as presented.
    pub(crate) pin: String,
    /// SHA-256 of the *whole* code string, which is what
    /// `robot_enrollment_codes.code_sha256` stores.
    pub(crate) code_sha256: [u8; 32],
}

impl ParsedEnrollmentCode {
    pub(crate) fn fingerprint(&self) -> String {
        fingerprint_of(&self.code_sha256)
    }
}

/// Parse and shape-check an enrollment code. No database, no allocation beyond
/// the pin, no timing that depends on the secret's value.
pub(crate) fn parse_enrollment_code(raw: &str) -> Result<ParsedEnrollmentCode, CodeFormatError> {
    if raw.is_empty() {
        return Err(CodeFormatError::Empty);
    }
    let Some(body) = raw.strip_prefix(ENROLLMENT_CODE_PREFIX) else {
        return Err(CodeFormatError::WrongVersion);
    };
    if raw.len() != ENROLLMENT_CODE_CHARS {
        return Err(CodeFormatError::WrongLength);
    }
    let Some((pin, secret)) = body.split_once('.') else {
        return Err(CodeFormatError::WrongShape);
    };
    if pin.len() != crockford::CA_PIN_CHARS || secret.len() != ENROLLMENT_SECRET_CHARS {
        return Err(CodeFormatError::WrongShape);
    }
    if !crockford::is_canonical_crockford(pin) || !crockford::is_canonical_crockford(secret) {
        return Err(CodeFormatError::NonCrockford);
    }
    Ok(ParsedEnrollmentCode {
        pin: pin.to_string(),
        code_sha256: sha256_of(raw),
    })
}

/// True when the presented bearer has the shape of a per-robot credential.
///
/// Used by the gRPC interceptor to decide whether a non-shared bearer is worth
/// deferring to the handler's asynchronous lookup. Shape only — a well-formed
/// but unknown credential is rejected later with the same message as garbage.
pub(crate) fn parse_robot_credential(raw: &str) -> Option<[u8; 32]> {
    let secret = raw.strip_prefix(ROBOT_CREDENTIAL_PREFIX)?;
    if raw.len() != ROBOT_CREDENTIAL_CHARS || secret.len() != ROBOT_CREDENTIAL_SECRET_CHARS {
        return None;
    }
    if !crockford::is_canonical_crockford(secret) {
        return None;
    }
    Some(sha256_of(raw))
}

pub(crate) fn sha256_of(value: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    hasher.finalize().into()
}

/// The non-secret display handle: `hex(sha256)[0:12]`. Revealing 48 bits of the
/// hash of a >=160-bit secret leaks nothing, and it is what appears in ingest
/// logs so an operator can name a credential without ever seeing one.
pub(crate) fn fingerprint_of(digest: &[u8; 32]) -> String {
    hex::encode(digest)[..12].to_string()
}

// ===== Minting ==============================================================

/// Read `count` bytes from the OS CSPRNG.
///
/// `/dev/urandom` directly rather than a new crate dependency: the platform
/// contract for both ends of this product is Linux x86_64,
/// this is the same source `getrandom` falls back to, and adding a dependency
/// to the ingest lockfile for 20 bytes is not a trade this crate needs to make.
/// A short read is an error, never silently-shorter entropy.
pub(crate) fn os_random_bytes(count: usize) -> std::io::Result<Vec<u8>> {
    use std::io::Read as _;
    let mut file = std::fs::File::open("/dev/urandom")?;
    let mut buffer = vec![0u8; count];
    file.read_exact(&mut buffer)?;
    Ok(buffer)
}

/// Mint `ign1.<pin>.<secret>` for a deployment whose CA pin is already known.
pub(crate) fn mint_enrollment_code(ca_pin: &str) -> std::io::Result<String> {
    let secret = crockford::encode(&os_random_bytes(ENROLLMENT_SECRET_BYTES)?);
    debug_assert_eq!(secret.len(), ENROLLMENT_SECRET_CHARS);
    Ok(format!("{ENROLLMENT_CODE_PREFIX}{ca_pin}.{secret}"))
}

/// Mint `ignr1.<secret>`.
pub(crate) fn mint_robot_credential() -> std::io::Result<String> {
    let secret = crockford::encode(&os_random_bytes(ROBOT_CREDENTIAL_SECRET_BYTES)?);
    debug_assert_eq!(secret.len(), ROBOT_CREDENTIAL_SECRET_CHARS);
    Ok(format!("{ROBOT_CREDENTIAL_PREFIX}{secret}"))
}

/// SHA-256 of the CA's SubjectPublicKeyInfo, taken from a PEM certificate.
///
/// The SPKI, not the certificate: the CA certificate may be reissued on the
/// same key, and outstanding enrollment codes must survive that.
/// `SubjectPublicKeyInfo::raw` is the same DER blob
/// `openssl x509 -noout -pubkey | openssl pkey -pubin -outform DER` emits, which
/// is what `/robot` hashes.
pub(crate) fn ca_spki_sha256_from_pem(pem_bytes: &[u8]) -> Result<[u8; 32], String> {
    let (_, pem) =
        parse_x509_pem(pem_bytes).map_err(|error| format!("ca_pem_parse_error: {error}"))?;
    if pem.label != "CERTIFICATE" {
        return Err(format!("ca_pem_wrong_label: {}", pem.label));
    }
    let (_, certificate) = parse_x509_certificate(&pem.contents)
        .map_err(|error| format!("ca_certificate_parse_error: {error}"))?;
    let mut hasher = Sha256::new();
    hasher.update(certificate.public_key().raw);
    Ok(hasher.finalize().into())
}

// ===== The authenticated identity ===========================================

/// The authenticated identity fed into the pre-existing ownership plumbing.
///
/// A bearer credential and a SPIFFE URI SAN are deliberately the same type so
/// both are constrained at all five `UploadService` call sites. `source` exists
/// for logs and for the mid-stream re-check's cross-check, never to weaken a check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AuthenticatedRobot {
    pub(crate) robot_id: String,
    pub(crate) source: AuthenticatedRobotSource,
}

impl AuthenticatedRobot {
    pub(crate) fn from_certificate(robot_id: impl Into<String>) -> Self {
        Self {
            robot_id: robot_id.into(),
            source: AuthenticatedRobotSource::ClientCertificate,
        }
    }

    pub(crate) fn from_bearer(robot_id: impl Into<String>) -> Self {
        Self {
            robot_id: robot_id.into(),
            source: AuthenticatedRobotSource::BearerCredential,
        }
    }

    pub(crate) fn robot_id(&self) -> &str {
        &self.robot_id
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AuthenticatedRobotSource {
    BearerCredential,
    ClientCertificate,
}

impl AuthenticatedRobotSource {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::BearerCredential => "bearer_credential",
            Self::ClientCertificate => "client_certificate",
        }
    }
}

/// Which credential class a request presented.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CredentialClass {
    /// `INGEST_AUTH_TOKEN` is unset, so authentication is disabled entirely.
    /// Reachable only under the `dev` profile — `token` and `mtls`
    /// both require the token at startup.
    AuthDisabled,
    /// The shared fleet token.
    Operator,
    /// A per-robot `ignr1.` credential.
    Robot,
}

/// A verified per-robot credential row joined to its robot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct VerifiedCredential {
    pub(crate) credential_id: Uuid,
    pub(crate) robot_id: String,
    pub(crate) robot_state: String,
    pub(crate) auth_mode: String,
    pub(crate) fingerprint: String,
}

/// Everything one RPC's authentication established.
#[derive(Debug, Clone)]
pub(crate) struct RequestIdentity {
    pub(crate) class: CredentialClass,
    /// `None` only for the operator/auth-disabled classes with no client
    /// certificate. Under `mtls` a certificate identity is mandatory, so
    /// `None` is unreachable there — the check that makes the
    /// "missing certificate identity must be refused" rule hold.
    pub(crate) robot: Option<AuthenticatedRobot>,
    pub(crate) credential_id: Option<Uuid>,
    /// The SHA-256 of the presented bearer, retained so a long-running stream
    /// can re-verify it at a chunk boundary without holding the plaintext.
    pub(crate) credential_sha256: Option<[u8; 32]>,
}

impl RequestIdentity {
    pub(crate) fn robot(&self) -> Option<&AuthenticatedRobot> {
        self.robot.as_ref()
    }
}

// ===== Verification predicates ==============================================

/// The two *independent* liveness predicates, as one pure function.
///
/// A credential is dead when `expires_at <= NOW()`, and separately when it is
/// superseded and past `superseded_grace_until`. The schema's
/// `robot_credentials_supersede_expiry` constraint holds the two instants equal
/// for a superseded row, so they cannot disagree — but both are still checked,
/// because a constraint added later than a row is not a proof about that row.
/// The production predicate lives in `classify_credential_row`; this is the
/// same call with the robot-state term satisfied, so the liveness tests below
/// exercise the shipped code path rather than a second copy of it.
#[cfg(test)]
pub(crate) fn credential_is_live(
    revoked_at: Option<chrono::DateTime<chrono::Utc>>,
    expires_at: Option<chrono::DateTime<chrono::Utc>>,
    superseded_at: Option<chrono::DateTime<chrono::Utc>>,
    superseded_grace_until: Option<chrono::DateTime<chrono::Utc>>,
    now: chrono::DateTime<chrono::Utc>,
) -> bool {
    classify_credential_row(
        revoked_at,
        expires_at,
        superseded_at,
        superseded_grace_until,
        ROBOT_STATE_ENROLLED,
        now,
    )
    .is_ok()
}

/// Uploads are permitted only from a robot the registry says is `enrolled`.
pub(crate) const ROBOT_STATE_ENROLLED: &str = "enrolled";
pub(crate) const ROBOT_STATE_PENDING: &str = "pending";
pub(crate) const ROBOT_STATE_DISABLED: &str = "disabled";
pub(crate) const ROBOT_STATE_DECOMMISSIONED: &str = "decommissioned";
pub(crate) const AUTH_MODE_SHARED: &str = "shared";
pub(crate) const AUTH_MODE_PER_ROBOT: &str = "per_robot";

// ===== Caches and revocation timing =========================================

/// Default positive-cache lifetime, and the ceiling on the mid-stream re-check
/// interval. The revocation guarantee — revocation effective for new RPCs within 30 s
/// and in-flight streams terminated within 30 s — *is* this number, so both the
/// cache and the re-check read it from here rather than each carrying a 30.
pub(crate) const CREDENTIAL_CACHE_TTL: Duration = Duration::from_secs(30);

#[derive(Debug, Clone)]
struct CachedCredential {
    verified: VerifiedCredential,
    cached_at: Instant,
}

/// Positive-only, TTL-bounded credential cache keyed by `SHA-256(token)`.
///
/// Negative results are never cached, so a newly issued credential works on its
/// first attempt. Ingest owns every lifecycle
/// write, so it invalidates synchronously on commit; the TTL is the backstop
/// for a future multi-replica deployment, not the primary mechanism.
#[derive(Clone)]
pub(crate) struct CredentialCache {
    entries: Arc<DashMap<[u8; 32], CachedCredential>>,
    ttl: Duration,
}

impl CredentialCache {
    pub(crate) fn new(ttl: Duration) -> Self {
        Self {
            entries: Arc::new(DashMap::new()),
            ttl,
        }
    }

    pub(crate) fn get(&self, token_sha256: &[u8; 32]) -> Option<VerifiedCredential> {
        let entry = self.entries.get(token_sha256)?;
        if entry.cached_at.elapsed() >= self.ttl {
            drop(entry);
            self.entries.remove(token_sha256);
            return None;
        }
        Some(entry.verified.clone())
    }

    pub(crate) fn insert(&self, token_sha256: [u8; 32], verified: VerifiedCredential) {
        self.entries.insert(
            token_sha256,
            CachedCredential {
                verified,
                cached_at: Instant::now(),
            },
        );
    }

    /// Drop every cached credential belonging to one robot. This is what makes
    /// `state='disabled'` a whole-robot kill switch rather than a per-credential
    /// one — including for a credential an attacker self-rotated into existence
    /// and that the operator has therefore never seen.
    pub(crate) fn invalidate_robot(&self, robot_id: &str) {
        self.entries
            .retain(|_, entry| entry.verified.robot_id != robot_id);
    }

    pub(crate) fn invalidate_credential(&self, credential_id: Uuid) {
        self.entries
            .retain(|_, entry| entry.verified.credential_id != credential_id);
    }

    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        self.entries.len()
    }
}

#[derive(Debug, Clone)]
struct CachedRobot {
    state: String,
    auth_mode: String,
    cached_at: Instant,
}

/// The registry state of one robot, as the upload path needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RobotRegistration {
    pub(crate) state: String,
    pub(crate) auth_mode: String,
}

impl RobotRegistration {
    pub(crate) fn is_enrolled(&self) -> bool {
        self.state == ROBOT_STATE_ENROLLED
    }
}

/// Entries expire, and they cache the lifecycle columns rather than mere
/// existence, so the state machine is enforced on the hot path: a `disabled`
/// robot stops uploading once its entry expires.
#[derive(Clone)]
pub(crate) struct RobotRegistryCache {
    entries: Arc<DashMap<String, CachedRobot>>,
    ttl: Duration,
}

impl RobotRegistryCache {
    pub(crate) fn new(ttl: Duration) -> Self {
        Self {
            entries: Arc::new(DashMap::new()),
            ttl,
        }
    }

    pub(crate) fn get(&self, robot_id: &str) -> Option<RobotRegistration> {
        let entry = self.entries.get(robot_id)?;
        if entry.cached_at.elapsed() >= self.ttl {
            drop(entry);
            self.entries.remove(robot_id);
            return None;
        }
        Some(RobotRegistration {
            state: entry.state.clone(),
            auth_mode: entry.auth_mode.clone(),
        })
    }

    pub(crate) fn insert(&self, robot_id: &str, registration: &RobotRegistration) {
        self.entries.insert(
            robot_id.to_string(),
            CachedRobot {
                state: registration.state.clone(),
                auth_mode: registration.auth_mode.clone(),
                cached_at: Instant::now(),
            },
        );
    }

    pub(crate) fn invalidate(&self, robot_id: &str) {
        self.entries.remove(robot_id);
    }
}

// ===== Database access ======================================================

/// Credential lookup and liveness check, as one indexed query.
///
/// The liveness predicates are in SQL so the index does the work, and are
/// re-asserted in Rust by `credential_is_live` against the returned timestamps
/// so a future query edit cannot quietly widen them.
pub(crate) const CREDENTIAL_LOOKUP_SQL: &str = "\
SELECT c.credential_id, c.robot_id, c.fingerprint, c.revoked_at, c.expires_at, \
       c.superseded_at, c.superseded_grace_until, r.state, r.auth_mode \
  FROM robot_credentials c \
  JOIN robots r ON r.id = c.robot_id \
 WHERE c.token_sha256 = $1";

/// Records that a credential is in use. One write per cache miss, i.e. at most
/// one per credential per TTL, which is what makes `first_used_at`/`last_used_at`
/// usable as the compromise-window anchor in an incident review without a write per RPC.
pub(crate) const CREDENTIAL_TOUCH_SQL: &str = "\
UPDATE robot_credentials \
   SET first_used_at = COALESCE(first_used_at, NOW()), last_used_at = NOW() \
 WHERE credential_id = $1";

pub(crate) const ROBOT_REGISTRATION_SQL: &str = "SELECT state, auth_mode FROM robots WHERE id = $1";

/// Why a presented per-robot credential was refused. Every variant maps to the
/// same client-visible status; the distinction is for metrics and the log line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CredentialRejection {
    Unknown,
    Revoked,
    Expired,
    SupersedeExpired,
    RobotNotEnrolled,
}

impl CredentialRejection {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Unknown => "unknown_credential",
            Self::Revoked => "revoked_credential",
            Self::Expired => "expired_credential",
            Self::SupersedeExpired => "supersede_grace_expired",
            Self::RobotNotEnrolled => "robot_not_enrolled",
        }
    }
}

/// Classify one credential row. Split out from the I/O so every liveness predicate
/// is unit-testable without a database.
#[allow(clippy::too_many_arguments)]
pub(crate) fn classify_credential_row(
    revoked_at: Option<chrono::DateTime<chrono::Utc>>,
    expires_at: Option<chrono::DateTime<chrono::Utc>>,
    superseded_at: Option<chrono::DateTime<chrono::Utc>>,
    superseded_grace_until: Option<chrono::DateTime<chrono::Utc>>,
    robot_state: &str,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<(), CredentialRejection> {
    if revoked_at.is_some() {
        return Err(CredentialRejection::Revoked);
    }
    if expires_at.is_some_and(|deadline| deadline <= now) {
        return Err(CredentialRejection::Expired);
    }
    if superseded_at.is_some() && superseded_grace_until.is_none_or(|deadline| deadline <= now) {
        return Err(CredentialRejection::SupersedeExpired);
    }
    if robot_state != ROBOT_STATE_ENROLLED {
        return Err(CredentialRejection::RobotNotEnrolled);
    }
    Ok(())
}

/// Look a credential up, honouring the cache. `Ok(None)` never happens: either
/// the credential verifies or the caller gets the rejection reason.
pub(crate) async fn verify_robot_credential(
    pool: &Pool,
    cache: &CredentialCache,
    token_sha256: &[u8; 32],
) -> Result<Result<VerifiedCredential, CredentialRejection>, String> {
    if let Some(cached) = cache.get(token_sha256) {
        return Ok(Ok(cached));
    }
    let client = pool
        .get()
        .await
        .map_err(|error| format!("credential_pool_error: {error}"))?;
    let row = client
        .query_opt(CREDENTIAL_LOOKUP_SQL, &[&token_sha256.as_slice()])
        .await
        .map_err(|error| format!("credential_query_error: {error}"))?;
    let Some(row) = row else {
        return Ok(Err(CredentialRejection::Unknown));
    };
    let credential_id: Uuid = row.get(0);
    let robot_id: String = row.get(1);
    let fingerprint: String = row.get(2);
    let revoked_at: Option<chrono::DateTime<chrono::Utc>> = row.get(3);
    let expires_at: Option<chrono::DateTime<chrono::Utc>> = row.get(4);
    let superseded_at: Option<chrono::DateTime<chrono::Utc>> = row.get(5);
    let superseded_grace_until: Option<chrono::DateTime<chrono::Utc>> = row.get(6);
    let robot_state: String = row.get(7);
    let auth_mode: String = row.get(8);
    if let Err(rejection) = classify_credential_row(
        revoked_at,
        expires_at,
        superseded_at,
        superseded_grace_until,
        &robot_state,
        chrono::Utc::now(),
    ) {
        return Ok(Err(rejection));
    }
    let verified = VerifiedCredential {
        credential_id,
        robot_id,
        robot_state,
        auth_mode,
        fingerprint,
    };
    // Best effort: a failed usage stamp must never fail an authenticated RPC.
    if let Err(error) = client
        .execute(CREDENTIAL_TOUCH_SQL, &[&credential_id])
        .await
    {
        tracing::warn!(%error, credential_id = %credential_id, "credential usage stamp failed");
    }
    cache.insert(*token_sha256, verified.clone());
    Ok(Ok(verified))
}

/// Read one robot's lifecycle row, honouring the TTL cache.
pub(crate) async fn lookup_robot_registration(
    pool: &Pool,
    cache: &RobotRegistryCache,
    robot_id: &str,
) -> Result<Option<RobotRegistration>, (Option<String>, String)> {
    if let Some(cached) = cache.get(robot_id) {
        return Ok(Some(cached));
    }
    let client = pool
        .get()
        .await
        .map_err(|error| (None, format!("robot_registry_pool_error: {error}")))?;
    let row = client
        .query_opt(ROBOT_REGISTRATION_SQL, &[&robot_id])
        .await
        .map_err(|error| {
            let sqlstate = error.code().map(|code| code.code().to_string());
            (sqlstate, format!("robot_registry_query_error: {error}"))
        })?;
    let Some(row) = row else {
        return Ok(None);
    };
    let registration = RobotRegistration {
        state: row.get(0),
        auth_mode: row.get(1),
    };
    cache.insert(robot_id, &registration);
    Ok(Some(registration))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration as ChronoDuration, Utc};

    fn valid_code() -> String {
        format!(
            "{ENROLLMENT_CODE_PREFIX}5NRHCGNQ4TR480B2FJMZQB1JYQ.{}",
            "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        )
    }

    fn valid_credential() -> String {
        format!(
            "{ROBOT_CREDENTIAL_PREFIX}{}",
            "0123456789ABCDEFGHJKMNPQRSTVWXYZ01234567890123456789"
        )
    }

    #[test]
    fn a_minted_code_round_trips_through_the_parser() {
        let code = mint_enrollment_code("5NRHCGNQ4TR480B2FJMZQB1JYQ").expect("mint");
        assert_eq!(code.len(), ENROLLMENT_CODE_CHARS);
        let parsed = parse_enrollment_code(&code).expect("minted codes must parse");
        assert_eq!(parsed.pin, "5NRHCGNQ4TR480B2FJMZQB1JYQ");
        assert_eq!(parsed.code_sha256, sha256_of(&code));
        assert_eq!(parsed.fingerprint().len(), 12);
        assert!(parsed.fingerprint().chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn two_minted_codes_differ() {
        // Not a statistical test — a stubbed-out CSPRNG returning zeros would
        // be caught here and nowhere else.
        let first = mint_enrollment_code("5NRHCGNQ4TR480B2FJMZQB1JYQ").expect("mint");
        let second = mint_enrollment_code("5NRHCGNQ4TR480B2FJMZQB1JYQ").expect("mint");
        assert_ne!(first, second);
        let credential = mint_robot_credential().expect("mint");
        assert_ne!(credential, mint_robot_credential().expect("mint"));
        assert_eq!(credential.len(), ROBOT_CREDENTIAL_CHARS);
        assert!(parse_robot_credential(&credential).is_some());
    }

    // ---- Shape rejection before any database access -------------------------

    #[test]
    fn code_parsing_rejects_every_malformed_shape() {
        let good = valid_code();
        assert!(parse_enrollment_code(&good).is_ok());
        assert_eq!(parse_enrollment_code(""), Err(CodeFormatError::Empty));
        assert_eq!(
            parse_enrollment_code(&good.replace("ign1.", "ign2.")),
            Err(CodeFormatError::WrongVersion)
        );
        assert_eq!(
            parse_enrollment_code(&good.replace("ign1.", "ignr1.")),
            Err(CodeFormatError::WrongVersion)
        );
        assert_eq!(
            parse_enrollment_code(&good[..63]),
            Err(CodeFormatError::WrongLength)
        );
        assert_eq!(
            parse_enrollment_code(&format!("{good}0")),
            Err(CodeFormatError::WrongLength)
        );
        // Right length, dot one character to the right.
        let misplaced = format!(
            "{ENROLLMENT_CODE_PREFIX}{}.{}",
            "5NRHCGNQ4TR480B2FJMZQB1JYQ0", "123456789ABCDEFGHJKMNPQRSTVWXYZ"
        );
        assert_eq!(misplaced.len(), ENROLLMENT_CODE_CHARS);
        assert_eq!(
            parse_enrollment_code(&misplaced),
            Err(CodeFormatError::WrongShape)
        );
        // No dot at all, right length.
        assert_eq!(
            parse_enrollment_code(&good.replacen('.', "0", 2)[..ENROLLMENT_CODE_CHARS]),
            Err(CodeFormatError::WrongVersion)
        );
        // Right length and shape, excluded Crockford letters.
        let non_crockford = good.replace('5', "U");
        assert_eq!(
            parse_enrollment_code(&non_crockford),
            Err(CodeFormatError::NonCrockford)
        );
        // Crockford is case-insensitive by design; this parser deliberately is
        // not, because two spellings of one code are two SHA-256 values.
        assert_eq!(
            parse_enrollment_code(&good.to_lowercase()),
            Err(CodeFormatError::NonCrockford)
        );
    }

    #[test]
    fn credential_parsing_rejects_every_malformed_shape() {
        assert!(parse_robot_credential(&valid_credential()).is_some());
        assert!(parse_robot_credential("").is_none());
        assert!(parse_robot_credential("ignr1.").is_none());
        // The enrollment code is not a credential and vice versa: the two
        // version tags are the whole point of disjoint credential classes.
        assert!(parse_robot_credential(&valid_code()).is_none());
        assert!(parse_enrollment_code(&valid_credential()).is_err());
        let one_short = valid_credential();
        assert!(parse_robot_credential(&one_short[..one_short.len() - 1]).is_none());
        assert!(parse_robot_credential(&format!("{one_short}0")).is_none());
        assert!(parse_robot_credential(&one_short.replace('0', "U")).is_none());
        assert!(parse_robot_credential("dev-ingest-token-change-me").is_none());
    }

    // ---- The two independent liveness predicates ----------------------------

    #[test]
    fn a_credential_with_no_expiry_stays_live_indefinitely() {
        let now = Utc::now();
        // The `Enroll`-issued shape: expires_at IS NULL. Advance a year.
        assert!(credential_is_live(
            None,
            None,
            None,
            None,
            now + ChronoDuration::days(365)
        ));
    }

    #[test]
    fn each_liveness_predicate_kills_a_credential_on_its_own() {
        let now = Utc::now();
        let past = now - ChronoDuration::seconds(1);
        let future = now + ChronoDuration::hours(1);
        assert!(
            !credential_is_live(Some(past), None, None, None, now),
            "revoked"
        );
        assert!(
            !credential_is_live(None, Some(past), None, None, now),
            "expired"
        );
        assert!(
            !credential_is_live(None, Some(future), Some(past), Some(past), now),
            "superseded grace elapsed even though expires_at is still in the future"
        );
        assert!(
            credential_is_live(None, Some(future), Some(past), Some(future), now),
            "inside the overlap window both predicates must pass"
        );
        // Exactly at the boundary the credential is dead: the predicate is
        // `<= now`, matching "dead when expires_at <= NOW()".
        assert!(!credential_is_live(None, Some(now), None, None, now));
    }

    #[test]
    fn classification_reports_the_first_failing_predicate_and_the_robot_state() {
        let now = Utc::now();
        let past = now - ChronoDuration::seconds(1);
        assert_eq!(
            classify_credential_row(None, None, None, None, ROBOT_STATE_ENROLLED, now),
            Ok(())
        );
        assert_eq!(
            classify_credential_row(Some(past), None, None, None, ROBOT_STATE_ENROLLED, now),
            Err(CredentialRejection::Revoked)
        );
        assert_eq!(
            classify_credential_row(None, Some(past), None, None, ROBOT_STATE_ENROLLED, now),
            Err(CredentialRejection::Expired)
        );
        assert_eq!(
            classify_credential_row(
                None,
                Some(past),
                Some(past),
                Some(past),
                ROBOT_STATE_ENROLLED,
                now
            ),
            Err(CredentialRejection::Expired)
        );
        assert_eq!(
            classify_credential_row(
                None,
                None,
                Some(past),
                Some(past),
                ROBOT_STATE_ENROLLED,
                now
            ),
            Err(CredentialRejection::SupersedeExpired)
        );
        // A perfectly live credential on a robot that is not enrolled is still
        // refused — this is what makes `disabled` a kill switch for every
        // credential of that robot, including one an attacker self-rotated.
        for state in [
            ROBOT_STATE_PENDING,
            ROBOT_STATE_DISABLED,
            ROBOT_STATE_DECOMMISSIONED,
        ] {
            assert_eq!(
                classify_credential_row(None, None, None, None, state, now),
                Err(CredentialRejection::RobotNotEnrolled),
                "state {state} must not authenticate"
            );
        }
    }

    // ---- caches ------------------------------------------------------------

    fn verified(robot_id: &str) -> VerifiedCredential {
        VerifiedCredential {
            credential_id: Uuid::new_v4(),
            robot_id: robot_id.to_string(),
            robot_state: ROBOT_STATE_ENROLLED.to_string(),
            auth_mode: AUTH_MODE_PER_ROBOT.to_string(),
            fingerprint: "a3f01c9e2b44".to_string(),
        }
    }

    #[test]
    fn the_credential_cache_expires_and_never_caches_a_miss() {
        let cache = CredentialCache::new(Duration::from_millis(40));
        let key = [7u8; 32];
        assert!(cache.get(&key).is_none());
        cache.insert(key, verified("rover-7"));
        assert_eq!(cache.get(&key).unwrap().robot_id, "rover-7");
        std::thread::sleep(Duration::from_millis(60));
        assert!(
            cache.get(&key).is_none(),
            "the entry must not outlive its TTL"
        );
        assert_eq!(cache.len(), 0, "an expired entry is dropped, not kept");
    }

    #[test]
    fn invalidating_a_robot_drops_every_credential_it_owns() {
        let cache = CredentialCache::new(CREDENTIAL_CACHE_TTL);
        let a_first = [1u8; 32];
        let a_second = [2u8; 32];
        let b_only = [3u8; 32];
        cache.insert(a_first, verified("rover-7"));
        cache.insert(a_second, verified("rover-7"));
        cache.insert(b_only, verified("rover-8"));
        cache.invalidate_robot("rover-7");
        assert!(cache.get(&a_first).is_none());
        assert!(cache.get(&a_second).is_none());
        assert!(
            cache.get(&b_only).is_some(),
            "another robot must be untouched"
        );
    }

    #[test]
    fn invalidating_one_credential_leaves_its_siblings_alone() {
        let cache = CredentialCache::new(CREDENTIAL_CACHE_TTL);
        let first = verified("rover-7");
        let second = verified("rover-7");
        cache.insert([1u8; 32], first.clone());
        cache.insert([2u8; 32], second.clone());
        cache.invalidate_credential(first.credential_id);
        assert!(cache.get(&[1u8; 32]).is_none());
        assert_eq!(
            cache.get(&[2u8; 32]).unwrap().credential_id,
            second.credential_id
        );
    }

    #[test]
    fn the_registry_cache_expires_so_a_disabled_robot_cannot_upload_forever() {
        // A disabled robot's cached registration must expire.
        let cache = RobotRegistryCache::new(Duration::from_millis(40));
        let enrolled = RobotRegistration {
            state: ROBOT_STATE_ENROLLED.to_string(),
            auth_mode: AUTH_MODE_PER_ROBOT.to_string(),
        };
        cache.insert("rover-7", &enrolled);
        assert!(cache.get("rover-7").unwrap().is_enrolled());
        std::thread::sleep(Duration::from_millis(60));
        assert!(cache.get("rover-7").is_none());
        cache.insert("rover-7", &enrolled);
        cache.invalidate("rover-7");
        assert!(
            cache.get("rover-7").is_none(),
            "a lifecycle write invalidates synchronously"
        );
    }

    // ---- CA pin ------------------------------------------------------------

    #[test]
    fn a_non_certificate_pem_is_refused_rather_than_hashed() {
        assert!(ca_spki_sha256_from_pem(b"not a pem at all").is_err());
        assert!(ca_spki_sha256_from_pem(
            b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        )
        .is_err());
    }
}
