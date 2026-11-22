//! `/admin/v2/robots*` — the provisioning API.
//!
//! This is the **only** surface `INGEST_PROVISIONING_TOKEN` authorizes, and the
//! operator token authorizes **none** of it. That disjointness is what makes
//! the provisioning boundary real rather than nominal: the query service proxies here with
//! a secret that cannot upload, cannot read the upload index or a manifest, and
//! cannot mint a credential it could use itself.
//!
//! Two rules that are easy to get wrong and are therefore stated here:
//!
//! * **No fail-open.** The development `POST /admin/robots` route treats an
//!   unset `INGEST_AUTH_TOKEN` as "auth disabled". That does not transfer to a
//!   route prefix that mints credentials, and inheriting it would let
//!   `INGEST_AUTH_TOKEN` reach `/admin/v2` on any deployment whose
//!   provisioning secret was forgotten —
//!   which must never happen. An unconfigured provisioning token
//!   makes every route here `401`.
//! * **`POST /admin/v2/robots` is not idempotent.** A duplicate id is `409`,
//!   never a silent success and never an implicit code mint: "Add robot:
//!   rover-7" when rover-7 is already live must not hand an operator a
//!   credential for a running robot because of a typo.

use axum::extract::{Path, Query};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{delete, post};
use axum::{Json, Router};
use deadpool_postgres::Pool;
use metrics::counter;
use serde_json::json;
use tracing::{info, warn};
use uuid::Uuid;

use crate::enrollment::{
    resolve_code_ttl_seconds, sanitize_display, LIFECYCLE_EVENT_INSERT_SQL,
    LIVE_CREDENTIAL_COUNT_SQL, MAX_OPEN_CODES_PER_ROBOT,
};
use crate::robot_auth::{
    self, CredentialCache, RobotRegistryCache, ROBOT_STATE_DECOMMISSIONED, ROBOT_STATE_DISABLED,
    ROBOT_STATE_ENROLLED, ROBOT_STATE_PENDING,
};

/// The audit header the query proxy sets from the admin's OIDC subject. Ingest
/// treats it as untrusted display metadata: query asserts it, ingest cannot
/// verify it.
pub(crate) const ACTOR_HEADER: &str = "x-ingotron-actor";

/// A credential minted through `Enroll` but never used is the detectable half
/// of the enrollment exchange's unremovable race ("the commit succeeded and the response was lost
/// in flight"). Minting a replacement code revokes those, which is the
/// operator's one-action fix. The age floor keeps a legitimate just-enrolled
/// robot that has not uploaded yet from being cut off by a second code mint.
pub(crate) const UNUSED_CREDENTIAL_REVOKE_AGE_MINUTES: i64 = 10;

#[derive(Clone)]
pub(crate) struct ProvisioningState {
    pub(crate) pool: Pool,
    pub(crate) credential_cache: CredentialCache,
    pub(crate) registry_cache: RobotRegistryCache,
    /// `None` when no CA certificate is readable: codes cannot be minted
    /// without a pin, and minting one with a placeholder would produce a code
    /// that fails closed at the robot for a reason nobody could diagnose.
    pub(crate) ca_pin: Option<String>,
    pub(crate) ca_spki_sha256_hex: Option<String>,
    pub(crate) ingest_endpoint: String,
}

// ===== Authorization ========================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ProvisioningAuth {
    Allowed,
    /// No `INGEST_PROVISIONING_TOKEN` is configured. Deliberately *not* an
    /// allow: see the module header.
    NotConfigured,
    Missing,
    Invalid,
}

/// The provisioning-plane twin of `evaluate_auth`, with the fail-open branch
/// removed. Pure, so its rules are unit tests.
pub(crate) fn evaluate_provisioning_auth(
    expected: Option<&str>,
    presented: Option<&str>,
) -> ProvisioningAuth {
    let Some(expected) = expected.map(str::trim).filter(|token| !token.is_empty()) else {
        return ProvisioningAuth::NotConfigured;
    };
    let Some(presented) = presented.map(str::trim) else {
        return ProvisioningAuth::Missing;
    };
    let (scheme, rest) = presented
        .split_once(char::is_whitespace)
        .unwrap_or((presented, ""));
    let token = if scheme.eq_ignore_ascii_case("Bearer") {
        rest.trim()
    } else {
        presented
    };
    if token.is_empty() {
        return ProvisioningAuth::Missing;
    }
    if crate::constant_time_eq(expected.as_bytes(), token.as_bytes()) {
        ProvisioningAuth::Allowed
    } else {
        ProvisioningAuth::Invalid
    }
}

// ===== State machine ==========================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StateTransitionError {
    /// The requested value is not a state the API may set.
    UnknownTarget,
    /// `decommissioned` is terminal: no API or command leaves it.
    /// Decommissioning is permanent. To use this hardware again, add it as a
    /// new robot.
    Decommissioned,
    /// A legal state, illegal from here — notably `pending → enrolled`, which
    /// is `Enroll`'s job and would also violate `robots_enrolled_at_check`.
    NotAllowed,
}

impl StateTransitionError {
    pub(crate) fn error_code(self) -> &'static str {
        match self {
            Self::UnknownTarget => "invalid_state",
            Self::Decommissioned => "robot_decommissioned",
            Self::NotAllowed => "invalid_state_transition",
        }
    }

    pub(crate) fn status(self) -> StatusCode {
        match self {
            Self::UnknownTarget => StatusCode::BAD_REQUEST,
            Self::Decommissioned | Self::NotAllowed => StatusCode::CONFLICT,
        }
    }
}

/// Which lifecycle audit event a transition writes. Keyed on the **effective**
/// destination, so an `enable` that restores `pending` is still audited as
/// `enabled` — the operator performed one action and should read one word.
pub(crate) fn transition_event(current: &str, effective: &str) -> &'static str {
    match (current, effective) {
        (_, ROBOT_STATE_DISABLED) => "disabled",
        (ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED | ROBOT_STATE_PENDING) => "enabled",
        (_, ROBOT_STATE_DECOMMISSIONED) => "decommissioned",
        _ => "state_changed",
    }
}

/// Resolve a requested state change into the state that is actually written.
///
/// The return value is the **effective** destination, which is not always the
/// requested one: `enable` means "restore the prior state".
/// `disabled → enrolled` is right
/// for the common case and wrong for a robot that was disabled while still
/// `pending` — it never enrolled, so `enrolled_at` is NULL and
/// `robots_enrolled_at_check` refuses the row. A 409 would strand that robot
/// with no way forward, so `enable` lands it back in `pending`, where a fresh
/// code can enroll it normally.
///
/// `ever_enrolled` is the disjunction the schema's constraint tests: a row
/// may be `enrolled` only if it is a development `shared` row or it has an
/// `enrolled_at`. Passing it in keeps this function pure while making the
/// database's own invariant the thing that decides, rather than a second,
/// hand-maintained copy of it.
pub(crate) fn evaluate_state_transition(
    current: &str,
    requested: &str,
    ever_enrolled: bool,
) -> Result<&'static str, StateTransitionError> {
    if !matches!(
        requested,
        ROBOT_STATE_DISABLED | ROBOT_STATE_ENROLLED | ROBOT_STATE_DECOMMISSIONED
    ) {
        return Err(StateTransitionError::UnknownTarget);
    }
    if current == ROBOT_STATE_DECOMMISSIONED {
        return Err(StateTransitionError::Decommissioned);
    }
    match (current, requested) {
        // `pending` is not a disabled robot, so there is nothing to restore:
        // asking to move it to `enrolled` is asking to skip the code exchange,
        // which the state machine never allows. Still a 409.
        (ROBOT_STATE_PENDING, ROBOT_STATE_ENROLLED) => Err(StateTransitionError::NotAllowed),
        // `enable`: restore the state the robot was disabled *from*.
        (ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED) if !ever_enrolled => Ok(ROBOT_STATE_PENDING),
        // A row that is already `enrolled` with no `enrolled_at` cannot exist —
        // `robots_enrolled_at_check` forbids it — so this arm is unreachable
        // through the database. It is here because the property test below
        // enumerates the whole cross product and found that without it the
        // function would happily *re-write* `enrolled` for such a row. An
        // inconsistent row is not something an idempotent no-op should launder.
        (_, ROBOT_STATE_ENROLLED) if !ever_enrolled => Err(StateTransitionError::NotAllowed),
        (ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED) => Ok(ROBOT_STATE_ENROLLED),
        (ROBOT_STATE_ENROLLED, ROBOT_STATE_ENROLLED) => Ok(ROBOT_STATE_ENROLLED),
        (_, ROBOT_STATE_DISABLED) => Ok(ROBOT_STATE_DISABLED),
        (_, ROBOT_STATE_DECOMMISSIONED) => Ok(ROBOT_STATE_DECOMMISSIONED),
        _ => Err(StateTransitionError::NotAllowed),
    }
}

// ===== Request bodies =======================================================

#[derive(serde::Deserialize)]
pub(crate) struct CreateRobotBody {
    robot_id: String,
    name: Option<String>,
    metadata: Option<serde_json::Value>,
}

#[derive(serde::Deserialize)]
pub(crate) struct MintCodeBody {
    ttl_seconds: Option<i64>,
    purpose: Option<String>,
}

#[derive(serde::Deserialize)]
pub(crate) struct StateBody {
    state: String,
    reason: Option<String>,
}

#[derive(serde::Deserialize, Default)]
pub(crate) struct RevokeBody {
    reason: Option<String>,
    /// A scheduled revocation sets `expires_at` without `revoked_at`.
    at: Option<String>,
}

#[derive(serde::Deserialize)]
pub(crate) struct ListQuery {
    robot_id: Option<String>,
}

type JsonReply = (StatusCode, Json<serde_json::Value>);

fn bad_request(error: &str, detail: impl std::fmt::Display) -> JsonReply {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": error, "detail": detail.to_string()})),
    )
}

fn unavailable(error: &str, detail: impl std::fmt::Display) -> JsonReply {
    warn!(%error, detail = %detail, "provisioning request failed");
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": error})),
    )
}

fn actor_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(ACTOR_HEADER)
        .and_then(|value| value.to_str().ok())
        .map(sanitize_display)
        .filter(|actor| !actor.is_empty())
}

// ===== Routes ===============================================================

pub(crate) fn router(state: ProvisioningState, provisioning_token: Option<String>) -> Router {
    let create_state = state.clone();
    let list_state = state.clone();
    let mint_state = state.clone();
    let delete_code_state = state.clone();
    let set_state_state = state.clone();
    let revoke_state = state;

    let routes = Router::new()
        .route(
            "/admin/v2/robots",
            post(
                move |headers: HeaderMap, Json(body): Json<CreateRobotBody>| {
                    let state = create_state.clone();
                    async move { create_robot(state, headers, body).await }
                },
            )
            .get(move |Query(query): Query<ListQuery>| {
                let state = list_state.clone();
                async move { list_robots(state, query).await }
            }),
        )
        .route(
            "/admin/v2/robots/:robot_id/enrollment-codes",
            post(
                move |Path(robot_id): Path<String>,
                      headers: HeaderMap,
                      body: Option<Json<MintCodeBody>>| {
                    let state = mint_state.clone();
                    async move { mint_code(state, robot_id, headers, body.map(|Json(b)| b)).await }
                },
            ),
        )
        .route(
            "/admin/v2/robots/:robot_id/enrollment-codes/:code_id",
            delete(
                move |Path((robot_id, code_id)): Path<(String, String)>, headers: HeaderMap| {
                    let state = delete_code_state.clone();
                    async move { revoke_code(state, robot_id, code_id, headers).await }
                },
            ),
        )
        .route(
            "/admin/v2/robots/:robot_id/state",
            post(
                move |Path(robot_id): Path<String>,
                      headers: HeaderMap,
                      Json(body): Json<StateBody>| {
                    let state = set_state_state.clone();
                    async move { set_state(state, robot_id, headers, body).await }
                },
            ),
        )
        .route(
            "/admin/v2/robots/:robot_id/credentials/:credential_id/revoke",
            post(
                move |Path((robot_id, credential_id)): Path<(String, String)>,
                      headers: HeaderMap,
                      body: Option<Json<RevokeBody>>| {
                    let state = revoke_state.clone();
                    async move {
                        revoke_credential(
                            state,
                            robot_id,
                            credential_id,
                            headers,
                            body.map(|Json(b)| b).unwrap_or_default(),
                        )
                        .await
                    }
                },
            ),
        );

    // `route_layer`, matching the operator admin route: an unknown path under
    // /admin/v2 still 404s rather than being answered with 401.
    routes.route_layer(axum::middleware::from_fn(
        move |request: axum::extract::Request, next: axum::middleware::Next| {
            let expected = provisioning_token.clone();
            async move {
                let outcome = {
                    let presented = request
                        .headers()
                        .get(crate::AUTH_HEADER)
                        .and_then(|value| value.to_str().ok());
                    evaluate_provisioning_auth(expected.as_deref(), presented)
                };
                match outcome {
                    ProvisioningAuth::Allowed => next.run(request).await,
                    rejected => {
                        counter!(
                            "ingest_provisioning_auth_rejected_total",
                            "reason" => match rejected {
                                ProvisioningAuth::NotConfigured => "not_configured",
                                ProvisioningAuth::Missing => "missing",
                                _ => "invalid",
                            }
                        )
                        .increment(1);
                        // One body for all three, so presenting the operator
                        // token is indistinguishable from presenting garbage.
                        (
                            StatusCode::UNAUTHORIZED,
                            Json(json!({"error": "invalid_provisioning_token"})),
                        )
                            .into_response()
                    }
                }
            }
        },
    ))
}

pub(crate) async fn create_robot(
    state: ProvisioningState,
    headers: HeaderMap,
    body: CreateRobotBody,
) -> JsonReply {
    let robot_id = match crate::validate_robot_id(&body.robot_id) {
        Ok(id) => id.to_string(),
        Err(error) => {
            return bad_request(error.label(), error.message(&body.robot_id));
        }
    };
    let name = body
        .name
        .map(|name| sanitize_display(&name))
        .filter(|name| !name.trim().is_empty())
        .unwrap_or_else(|| robot_id.clone());
    let actor = actor_of(&headers);
    let mut client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let transaction = match client.transaction().await {
        Ok(transaction) => transaction,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    // Not idempotent by design: report the current state instead.
    let existing = match transaction
        .query_opt(
            "SELECT state, auth_mode FROM robots WHERE id = $1 FOR UPDATE",
            &[&robot_id],
        )
        .await
    {
        Ok(row) => row,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    if let Some(existing) = existing {
        let live: i64 = match transaction
            .query_one(LIVE_CREDENTIAL_COUNT_SQL, &[&robot_id])
            .await
        {
            Ok(row) => row.get(0),
            Err(error) => return unavailable("provisioning_unavailable", error),
        };
        return (
            StatusCode::CONFLICT,
            Json(json!({
                "error": "robot_id_exists",
                "state": existing.get::<_, String>(0),
                "auth_mode": existing.get::<_, String>(1),
                "active_credentials": live,
            })),
        );
    }
    if let Err(error) = transaction
        .execute(
            "INSERT INTO robots (id, name, metadata, state, auth_mode, state_changed_at, updated_at) \
             VALUES ($1, $2, $3, 'pending', 'per_robot', NOW(), NOW())",
            &[&robot_id, &name, &body.metadata],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    if let Err(error) = transaction
        .execute(
            LIFECYCLE_EVENT_INSERT_SQL,
            &[&robot_id, &"created", &actor, &json!({"name": name})],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    let created_at: chrono::DateTime<chrono::Utc> = match transaction
        .query_one("SELECT created_at FROM robots WHERE id = $1", &[&robot_id])
        .await
    {
        Ok(row) => row.get(0),
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    if let Err(error) = transaction.commit().await {
        return unavailable("provisioning_unavailable", error);
    }
    state.registry_cache.invalidate(&robot_id);
    info!(robot_id = %robot_id, "robot created via /admin/v2");
    (
        StatusCode::CREATED,
        Json(json!({
            "robot_id": robot_id,
            "state": ROBOT_STATE_PENDING,
            "auth_mode": robot_auth::AUTH_MODE_PER_ROBOT,
            "created_at": created_at.to_rfc3339(),
        })),
    )
}

/// `GET /admin/v2/robots` returns **no secrets** — hashes are never serialized,
/// only the 12-character fingerprints.
pub(crate) async fn list_robots(state: ProvisioningState, query: ListQuery) -> JsonReply {
    let client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let filter = query.robot_id.unwrap_or_default();
    let rows = if filter.is_empty() {
        client
            .query(
                "SELECT id, name, state, auth_mode, enrolled_at FROM robots ORDER BY id",
                &[],
            )
            .await
    } else {
        client
            .query(
                "SELECT id, name, state, auth_mode, enrolled_at FROM robots WHERE id = $1",
                &[&filter],
            )
            .await
    };
    let rows = match rows {
        Ok(rows) => rows,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let mut robots = Vec::with_capacity(rows.len());
    for row in rows {
        let robot_id: String = row.get(0);
        let enrolled_at: Option<chrono::DateTime<chrono::Utc>> = row.get(4);
        let credential_rows = match client
            .query(
                "SELECT credential_id, fingerprint, issued_at, issued_via, first_used_at, \
                        last_used_at, superseded_at, revoked_at, expires_at \
                   FROM robot_credentials WHERE robot_id = $1 ORDER BY issued_at",
                &[&robot_id],
            )
            .await
        {
            Ok(rows) => rows,
            Err(error) => return unavailable("provisioning_unavailable", error),
        };
        let credentials: Vec<serde_json::Value> = credential_rows
            .into_iter()
            .map(|credential| {
                let stamp = |index: usize| {
                    credential
                        .get::<_, Option<chrono::DateTime<chrono::Utc>>>(index)
                        .map(|value| value.to_rfc3339())
                };
                json!({
                    "credential_id": credential.get::<_, Uuid>(0).to_string(),
                    "fingerprint": credential.get::<_, String>(1),
                    "issued_at": credential.get::<_, chrono::DateTime<chrono::Utc>>(2).to_rfc3339(),
                    "issued_via": credential.get::<_, String>(3),
                    "first_used_at": stamp(4),
                    "last_used_at": stamp(5),
                    "superseded_at": stamp(6),
                    "revoked_at": stamp(7),
                    "expires_at": stamp(8),
                })
            })
            .collect();
        let open_codes: i64 = match client
            .query_one(
                "SELECT COUNT(*) FROM robot_enrollment_codes \
                  WHERE robot_id = $1 AND consumed_at IS NULL AND revoked_at IS NULL \
                    AND expires_at > NOW()",
                &[&robot_id],
            )
            .await
        {
            Ok(row) => row.get(0),
            Err(error) => return unavailable("provisioning_unavailable", error),
        };
        robots.push(json!({
            "robot_id": robot_id,
            "name": row.get::<_, String>(1),
            "state": row.get::<_, String>(2),
            "auth_mode": row.get::<_, String>(3),
            "enrolled_at": enrolled_at.map(|value| value.to_rfc3339()),
            "credentials": credentials,
            "open_codes": open_codes,
        }));
    }
    (StatusCode::OK, Json(json!({"robots": robots})))
}

pub(crate) async fn mint_code(
    state: ProvisioningState,
    robot_id: String,
    headers: HeaderMap,
    body: Option<MintCodeBody>,
) -> JsonReply {
    let robot_id = match crate::validate_robot_id(&robot_id) {
        Ok(id) => id.to_string(),
        Err(error) => return bad_request(error.label(), error.message(&robot_id)),
    };
    let Some(ca_pin) = state.ca_pin.clone() else {
        // Without a CA pin the code cannot carry the trust anchor the robot
        // verifies before sending the secret. Refusing is the only honest
        // answer; minting a pinless code would fail at the robot with no
        // diagnosable cause.
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "ca_certificate_unavailable"})),
        );
    };
    let body = body.unwrap_or(MintCodeBody {
        ttl_seconds: None,
        purpose: None,
    });
    let purpose = body.purpose.unwrap_or_else(|| "enroll".to_string());
    if !matches!(purpose.as_str(), "enroll" | "reenroll") {
        return bad_request("invalid_purpose", "purpose must be enroll or reenroll");
    }
    let ttl_seconds = resolve_code_ttl_seconds(body.ttl_seconds);
    let actor = actor_of(&headers);

    let mut client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let transaction = match client.transaction().await {
        Ok(transaction) => transaction,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let robot_row = match transaction
        .query_opt(
            "SELECT state FROM robots WHERE id = $1 FOR UPDATE",
            &[&robot_id],
        )
        .await
    {
        Ok(row) => row,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let Some(robot_row) = robot_row else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "robot_not_found"})),
        );
    };
    let current_state: String = robot_row.get(0);
    if current_state == ROBOT_STATE_DECOMMISSIONED {
        return (
            StatusCode::CONFLICT,
            Json(json!({"error": "robot_decommissioned"})),
        );
    }
    let open_codes: i64 = match transaction
        .query_one(
            "SELECT COUNT(*) FROM robot_enrollment_codes \
              WHERE robot_id = $1 AND consumed_at IS NULL AND revoked_at IS NULL \
                AND expires_at > NOW()",
            &[&robot_id],
        )
        .await
    {
        Ok(row) => row.get(0),
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    if open_codes >= MAX_OPEN_CODES_PER_ROBOT {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Json(json!({"error": "too_many_open_codes"})),
        );
    }
    // The stranded-credential repair, in the same transaction as the mint.
    let revoked_unused = match transaction
        .execute(
            "UPDATE robot_credentials \
                SET revoked_at = NOW(), revoked_reason = 'replaced_by_new_enrollment_code' \
              WHERE robot_id = $1 AND revoked_at IS NULL AND first_used_at IS NULL \
                AND issued_at < NOW() - ($2 || ' minutes')::interval",
            &[&robot_id, &UNUSED_CREDENTIAL_REVOKE_AGE_MINUTES.to_string()],
        )
        .await
    {
        Ok(count) => count,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };

    let code = match robot_auth::mint_enrollment_code(&ca_pin) {
        Ok(code) => code,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let code_sha256 = robot_auth::sha256_of(&code);
    let fingerprint = robot_auth::fingerprint_of(&code_sha256);
    let code_row = match transaction
        .query_one(
            "INSERT INTO robot_enrollment_codes \
                 (robot_id, code_sha256, fingerprint, purpose, created_by, expires_at) \
             VALUES ($1, $2, $3, $4, $5, NOW() + ($6 || ' seconds')::interval) \
             RETURNING code_id, expires_at",
            &[
                &robot_id,
                &code_sha256.as_slice(),
                &fingerprint,
                &purpose,
                &actor,
                &ttl_seconds.to_string(),
            ],
        )
        .await
    {
        Ok(row) => row,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let code_id: Uuid = code_row.get(0);
    let expires_at: chrono::DateTime<chrono::Utc> = code_row.get(1);
    if let Err(error) = transaction
        .execute(
            LIFECYCLE_EVENT_INSERT_SQL,
            &[
                &robot_id,
                &"code_minted",
                &actor,
                &json!({
                    "code_id": code_id.to_string(),
                    "fingerprint": fingerprint,
                    "purpose": purpose,
                    "ttl_seconds": ttl_seconds,
                    "revoked_unused_credentials": revoked_unused,
                }),
            ],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    if let Err(error) = transaction.commit().await {
        return unavailable("provisioning_unavailable", error);
    }
    if revoked_unused > 0 {
        state.credential_cache.invalidate_robot(&robot_id);
    }
    // The plaintext code exists in this response and the operator's clipboard,
    // and nowhere else — only the fingerprint is logged.
    info!(
        robot_id = %robot_id,
        code_fingerprint = %fingerprint,
        %purpose,
        "enrollment code minted"
    );
    counter!("ingest_enrollment_codes_minted_total").increment(1);
    let mut response = json!({
        "code": code,
        "code_id": code_id.to_string(),
        "fingerprint": fingerprint,
        "expires_at": expires_at.to_rfc3339(),
        "ca_spki_sha256": state.ca_spki_sha256_hex,
        "vault_ingest_endpoint": state.ingest_endpoint,
    });
    if current_state == ROBOT_STATE_ENROLLED {
        response["warning"] = json!("robot_is_already_enrolled");
    }
    (StatusCode::CREATED, Json(response))
}

pub(crate) async fn revoke_code(
    state: ProvisioningState,
    robot_id: String,
    code_id: String,
    headers: HeaderMap,
) -> axum::response::Response {
    let Ok(code_id) = code_id.parse::<Uuid>() else {
        return bad_request("invalid_code_id", "code_id must be a UUID").into_response();
    };
    let actor = actor_of(&headers);
    let mut client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error).into_response(),
    };
    let transaction = match client.transaction().await {
        Ok(transaction) => transaction,
        Err(error) => return unavailable("provisioning_unavailable", error).into_response(),
    };
    // The consuming UPDATE requires `revoked_at IS NULL`, so this is race-free:
    // a redemption in flight either already committed or now fails.
    let affected = match transaction
        .execute(
            "UPDATE robot_enrollment_codes \
                SET revoked_at = NOW(), revoked_reason = 'revoked_by_operator' \
              WHERE code_id = $1 AND robot_id = $2 AND revoked_at IS NULL AND consumed_at IS NULL",
            &[&code_id, &robot_id],
        )
        .await
    {
        Ok(affected) => affected,
        Err(error) => return unavailable("provisioning_unavailable", error).into_response(),
    };
    if affected > 0 {
        if let Err(error) = transaction
            .execute(
                LIFECYCLE_EVENT_INSERT_SQL,
                &[
                    &robot_id,
                    &"code_revoked",
                    &actor,
                    &json!({"code_id": code_id.to_string()}),
                ],
            )
            .await
        {
            return unavailable("provisioning_unavailable", error).into_response();
        }
    }
    if let Err(error) = transaction.commit().await {
        return unavailable("provisioning_unavailable", error).into_response();
    }
    // 204 either way: an already-revoked or already-consumed code is not a
    // state an operator can act on, and distinguishing them here would leak
    // whether a code had been redeemed to a caller who only holds its id.
    StatusCode::NO_CONTENT.into_response()
}

pub(crate) async fn set_state(
    state: ProvisioningState,
    robot_id: String,
    headers: HeaderMap,
    body: StateBody,
) -> JsonReply {
    let actor = actor_of(&headers);
    let reason = body.reason.map(|reason| sanitize_display(&reason));
    let mut client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let transaction = match client.transaction().await {
        Ok(transaction) => transaction,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let row = match transaction
        .query_opt(
            "SELECT state, auth_mode, enrolled_at FROM robots WHERE id = $1 FOR UPDATE",
            &[&robot_id],
        )
        .await
    {
        Ok(row) => row,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let Some(row) = row else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "robot_not_found"})),
        );
    };
    let current: String = row.get(0);
    let ever_enrolled = row.get::<_, String>(1) == robot_auth::AUTH_MODE_SHARED
        || row
            .get::<_, Option<chrono::DateTime<chrono::Utc>>>(2)
            .is_some();
    let effective = match evaluate_state_transition(&current, &body.state, ever_enrolled) {
        Ok(effective) => effective,
        Err(error) => {
            return (
                error.status(),
                Json(json!({"error": error.error_code(), "state": current})),
            )
        }
    };
    if let Err(error) = transaction
        .execute(
            "UPDATE robots SET state = $2, state_reason = $3, state_changed_at = NOW(), \
                    updated_at = NOW() WHERE id = $1",
            &[&robot_id, &effective, &reason],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    // Decommissioning revokes every credential: all credentials are
    // revoked, because leaving one live would make a terminal state a soft one.
    let revoked = if effective == ROBOT_STATE_DECOMMISSIONED {
        match transaction
            .execute(
                "UPDATE robot_credentials SET revoked_at = NOW(), \
                        revoked_reason = 'robot_decommissioned' \
                  WHERE robot_id = $1 AND revoked_at IS NULL",
                &[&robot_id],
            )
            .await
        {
            Ok(count) => count,
            Err(error) => return unavailable("provisioning_unavailable", error),
        }
    } else {
        0
    };
    let event = transition_event(&current, effective);
    if let Err(error) = transaction
        .execute(
            LIFECYCLE_EVENT_INSERT_SQL,
            &[
                &robot_id,
                &event,
                &actor,
                &json!({"from": current, "requested": body.state, "to": effective,
                        "reason": reason, "revoked_credentials": revoked}),
            ],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    if let Err(error) = transaction.commit().await {
        return unavailable("provisioning_unavailable", error);
    }
    // Synchronous invalidation on the commit path: this is what makes disable
    // effective for new RPCs immediately rather than in up to 30 s.
    state.registry_cache.invalidate(&robot_id);
    state.credential_cache.invalidate_robot(&robot_id);
    info!(
        robot_id = %robot_id, from = %current, requested = %body.state, to = %effective,
        "robot state changed"
    );
    (
        StatusCode::OK,
        // `state` is the state that was WRITTEN, which is what the caller has to
        // act on: an `enable` of a robot that never enrolled reports `pending`,
        // not the `enrolled` that was asked for.
        Json(json!({
            "robot_id": robot_id,
            "state": effective,
            "requested_state": body.state,
            "previous_state": current,
        })),
    )
}

pub(crate) async fn revoke_credential(
    state: ProvisioningState,
    robot_id: String,
    credential_id: String,
    headers: HeaderMap,
    body: RevokeBody,
) -> JsonReply {
    let Ok(credential_id) = credential_id.parse::<Uuid>() else {
        return bad_request("invalid_credential_id", "credential_id must be a UUID");
    };
    // `at` in the past or now is APPLIED IMMEDIATELY, never rejected.
    //
    // The schema's `robot_credentials_expiry_after_issue` CHECK makes a past
    // instant unrepresentable as a scheduled expiry, but a `400 invalid_at`
    // would be the wrong failure direction for this operation: revocation is
    // a containment action taken during an incident, and an operator who
    // types "revoke at 14:00" at 14:05
    // means *revoke now*. A 400 there leaves a live credential on a robot the
    // operator believes they have just cut off. The immediate path sets
    // `revoked_at` and never touches `expires_at`, so the CHECK is not engaged
    // at all. Only a malformed `at` is still a 400.
    let scheduled = match body.at.as_deref() {
        None => None,
        Some(raw) => match chrono::DateTime::parse_from_rfc3339(raw) {
            Ok(value) => {
                Some(value.with_timezone(&chrono::Utc)).filter(|at| *at > chrono::Utc::now())
            }
            Err(error) => return bad_request("invalid_at", error),
        },
    };
    let actor = actor_of(&headers);
    let reason = body
        .reason
        .map(|reason| sanitize_display(&reason))
        .unwrap_or_else(|| "revoked_by_operator".to_string());
    let mut client = match state.pool.get().await {
        Ok(client) => client,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    let transaction = match client.transaction().await {
        Ok(transaction) => transaction,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    // With `at`, this is a scheduled revocation — `expires_at` moves and
    // `revoked_at` stays NULL, so the credential dies at that instant by the
    // expiry predicate rather than by the revocation one.
    let affected = match scheduled {
        Some(at) => {
            transaction
                .execute(
                    "UPDATE robot_credentials SET expires_at = $3 \
                      WHERE credential_id = $1 AND robot_id = $2 AND revoked_at IS NULL",
                    &[&credential_id, &robot_id, &at],
                )
                .await
        }
        None => {
            transaction
                .execute(
                    "UPDATE robot_credentials SET revoked_at = NOW(), revoked_reason = $3 \
                      WHERE credential_id = $1 AND robot_id = $2 AND revoked_at IS NULL",
                    &[&credential_id, &robot_id, &reason],
                )
                .await
        }
    };
    let affected = match affected {
        Ok(affected) => affected,
        Err(error) => return unavailable("provisioning_unavailable", error),
    };
    if affected == 0 {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "credential_not_found"})),
        );
    }
    let applied = if scheduled.is_some() {
        "scheduled"
    } else {
        "immediate"
    };
    if let Err(error) = transaction
        .execute(
            LIFECYCLE_EVENT_INSERT_SQL,
            &[
                &robot_id,
                &"credential_revoked",
                &actor,
                &json!({
                    "credential_id": credential_id.to_string(),
                    "reason": reason,
                    "applied": applied,
                    "requested_at": body.at,
                    "scheduled_for": scheduled.map(|at| at.to_rfc3339()),
                }),
            ],
        )
        .await
    {
        return unavailable("provisioning_unavailable", error);
    }
    if let Err(error) = transaction.commit().await {
        return unavailable("provisioning_unavailable", error);
    }
    state.credential_cache.invalidate_credential(credential_id);
    info!(
        robot_id = %robot_id, credential_id = %credential_id, %applied,
        "credential revoked"
    );
    (
        StatusCode::OK,
        Json(json!({
            "credential_id": credential_id.to_string(),
            // `applied` is the operator-facing fact: a past `at` is honoured as
            // an immediate revocation, and saying so is what stops them
            // assuming a schedule that was never set.
            "applied": applied,
            "revoked": scheduled.is_none(),
            "expires_at": scheduled.map(|at| at.to_rfc3339()),
        })),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const OPERATOR: &str = "operator-token-that-is-long-enough-1234";
    const PROVISIONING: &str = "provisioning-token-that-is-long-enough-1";

    // ---- Provisioning-plane authentication ----------------------------------

    #[test]
    fn the_operator_token_does_not_reach_the_provisioning_plane() {
        // This is what makes the provisioning boundary real rather than nominal.
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(&format!("Bearer {OPERATOR}"))),
            ProvisioningAuth::Invalid
        );
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(OPERATOR)),
            ProvisioningAuth::Invalid
        );
    }

    #[test]
    fn a_robot_credential_does_not_reach_the_provisioning_plane() {
        // A robot credential is refused here as well.
        let credential = crate::robot_auth::mint_robot_credential().expect("mint");
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(&format!("Bearer {credential}"))),
            ProvisioningAuth::Invalid
        );
    }

    #[test]
    fn the_provisioning_plane_never_fails_open() {
        // The mutation this test exists to kill: reusing `evaluate_auth`, whose
        // `expected == None` branch is `Allowed`. Here it must be a refusal, or
        // a deployment that forgot the secret would accept the operator token.
        assert_eq!(
            evaluate_provisioning_auth(None, Some(&format!("Bearer {OPERATOR}"))),
            ProvisioningAuth::NotConfigured
        );
        assert_eq!(
            evaluate_provisioning_auth(None, None),
            ProvisioningAuth::NotConfigured
        );
        assert_eq!(
            evaluate_provisioning_auth(Some("   "), Some("anything")),
            ProvisioningAuth::NotConfigured
        );
        // And the operator-token helper really does differ, so the two are not
        // interchangeable by accident.
        assert_eq!(
            crate::evaluate_auth(None, Some("anything")),
            crate::AuthOutcome::Allowed
        );
    }

    #[test]
    fn provisioning_auth_accepts_the_configured_token_in_both_header_forms() {
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(PROVISIONING)),
            ProvisioningAuth::Allowed
        );
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(&format!("Bearer {PROVISIONING}"))),
            ProvisioningAuth::Allowed
        );
        assert_eq!(
            evaluate_provisioning_auth(
                Some(PROVISIONING),
                Some(&format!("bearer  {PROVISIONING} "))
            ),
            ProvisioningAuth::Allowed
        );
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), None),
            ProvisioningAuth::Missing
        );
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some("Bearer ")),
            ProvisioningAuth::Missing
        );
        // A same-length near miss must not be accepted, and the comparison that
        // rejects it is constant time.
        let mut near = PROVISIONING.to_string();
        near.pop();
        near.push('X');
        assert_eq!(
            evaluate_provisioning_auth(Some(PROVISIONING), Some(&near)),
            ProvisioningAuth::Invalid
        );
    }

    // ---- State machine ---------------------------------------------------

    #[test]
    fn decommissioned_is_terminal_to_the_api() {
        for target in [
            ROBOT_STATE_ENROLLED,
            ROBOT_STATE_DISABLED,
            ROBOT_STATE_DECOMMISSIONED,
        ] {
            for ever_enrolled in [true, false] {
                assert_eq!(
                    evaluate_state_transition(ROBOT_STATE_DECOMMISSIONED, target, ever_enrolled),
                    Err(StateTransitionError::Decommissioned),
                    "decommissioned -> {target} must be refused by the API"
                );
            }
        }
        assert_eq!(
            StateTransitionError::Decommissioned.status(),
            StatusCode::CONFLICT
        );
    }

    #[test]
    fn pending_cannot_be_promoted_to_enrolled_by_the_api() {
        // Enroll's job, and also what `robots_enrolled_at_check` would refuse:
        // a pending per_robot row has no enrolled_at.
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_PENDING, ROBOT_STATE_ENROLLED, false),
            Err(StateTransitionError::NotAllowed)
        );
    }

    #[test]
    fn enable_restores_the_state_the_robot_was_disabled_from() {
        // `enable` is "restore the prior state", not "go to
        // enrolled". A robot disabled while still `pending` never enrolled, so
        // `enrolled_at` is NULL; landing it in `enrolled` would break
        // `robots_enrolled_at_check`, and refusing it would strand it.
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED, false),
            Ok(ROBOT_STATE_PENDING),
            "a robot that never enrolled is restored to pending, not refused"
        );
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED, true),
            Ok(ROBOT_STATE_ENROLLED),
            "a robot that HAS enrolled is re-enabled with its credentials intact"
        );
        // The refusal that remains: `pending` is not a disabled robot, so there
        // is no prior state to restore and skipping the code exchange is still
        // the one thing the state machine does not allow.
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_PENDING, ROBOT_STATE_ENROLLED, false),
            Err(StateTransitionError::NotAllowed)
        );
    }

    #[test]
    fn the_reversible_kill_switch_is_allowed_in_both_directions() {
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_ENROLLED, ROBOT_STATE_DISABLED, true),
            Ok(ROBOT_STATE_DISABLED)
        );
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED, true),
            Ok(ROBOT_STATE_ENROLLED)
        );
        assert_eq!(
            evaluate_state_transition(ROBOT_STATE_PENDING, ROBOT_STATE_DISABLED, false),
            Ok(ROBOT_STATE_DISABLED)
        );
        for from in [
            ROBOT_STATE_PENDING,
            ROBOT_STATE_ENROLLED,
            ROBOT_STATE_DISABLED,
        ] {
            for ever_enrolled in [true, false] {
                assert_eq!(
                    evaluate_state_transition(from, ROBOT_STATE_DECOMMISSIONED, ever_enrolled),
                    Ok(ROBOT_STATE_DECOMMISSIONED),
                    "decommissioning never depends on having enrolled"
                );
            }
        }
    }

    #[test]
    fn every_permitted_transition_writes_a_state_the_check_constraint_accepts() {
        // The invariant the schema enforces, restated as a property over the
        // whole transition table: `state = 'enrolled'` is only ever written for
        // a robot that has an `enrolled_at` (or is a development `shared` row).
        for from in [
            ROBOT_STATE_PENDING,
            ROBOT_STATE_ENROLLED,
            ROBOT_STATE_DISABLED,
            ROBOT_STATE_DECOMMISSIONED,
        ] {
            for requested in [
                ROBOT_STATE_PENDING,
                ROBOT_STATE_ENROLLED,
                ROBOT_STATE_DISABLED,
                ROBOT_STATE_DECOMMISSIONED,
                "nonsense",
            ] {
                for ever_enrolled in [true, false] {
                    if let Ok(effective) = evaluate_state_transition(from, requested, ever_enrolled)
                    {
                        assert!(
                            effective != ROBOT_STATE_ENROLLED || ever_enrolled,
                            "{from} -> {requested} (ever_enrolled={ever_enrolled}) wrote                              `enrolled` for a robot that never enrolled"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn an_unknown_target_state_is_a_four_hundred_not_a_silent_write() {
        for target in ["pending", "ENROLLED", "", "deleted", "decommissioned "] {
            assert_eq!(
                evaluate_state_transition(ROBOT_STATE_ENROLLED, target, true),
                Err(StateTransitionError::UnknownTarget),
                "{target:?} must not be settable"
            );
        }
        assert_eq!(
            StateTransitionError::UnknownTarget.status(),
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn transitions_are_audited_under_the_operators_vocabulary() {
        assert_eq!(
            transition_event(ROBOT_STATE_ENROLLED, ROBOT_STATE_DISABLED),
            "disabled"
        );
        assert_eq!(
            transition_event(ROBOT_STATE_DISABLED, ROBOT_STATE_ENROLLED),
            "enabled"
        );
        assert_eq!(
            transition_event(ROBOT_STATE_DISABLED, ROBOT_STATE_PENDING),
            "enabled",
            "one operator action reads as one word, whichever state it restored"
        );
        assert_eq!(
            transition_event(ROBOT_STATE_DISABLED, ROBOT_STATE_DECOMMISSIONED),
            "decommissioned"
        );
    }
}
