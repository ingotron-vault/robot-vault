/*
 * The query service's robot-provisioning API.
 *
 * The query-side routes are narrow `httpx` proxies of the ingest
 * provisioning API (`/admin/v2/robots*`), all behind
 * `Depends(auth_mod.require_role("admin"))`:
 *
 *     POST   /api/admin/robots                        -> POST /admin/v2/robots
 *     POST   /api/admin/robots/{id}/enrollment-code   -> the mint, + install_command
 *     DELETE /api/admin/robots/{id}/enrollment-code/{code_id}
 *     POST   /api/admin/robots/{id}/disable | /enable | /decommission
 *     POST   /api/admin/robots/{id}/credentials/{cred_id}/revoke
 *     GET    /api/admin/robots/lifecycle              -> GET /admin/v2/robots
 *
 * Ingest's `:8080` is compose-network-only — a browser cannot
 * reach it and must not try.
 *
 * Every type below is transcribed from the landed ingest handlers' `json!{}`
 * blocks, because the proxy is a pass-through: whatever ingest serialises is
 * what arrives here. Note two details: credentials carry `expires_at`, and the
 * state route answers with `requested_state`/`previous_state` alongside
 * `state`.
 */
import { http, HttpError } from "./api";
import type { RobotState } from "./enrollment";

export const ROBOTS_LIFECYCLE_PATH = "/api/admin/robots/lifecycle";
export const ROBOTS_CREATE_PATH = "/api/admin/robots";

/** Ingest's per-credential item, including the `expires_at` it sends. */
export interface RobotCredential {
  credential_id: string;
  /** `hex(sha256(token))[0:12]`. A hash prefix, never the token. */
  fingerprint: string;
  issued_at: string;
  issued_via: string;
  first_used_at: string | null;
  last_used_at: string | null;
  superseded_at: string | null;
  revoked_at: string | null;
  expires_at?: string | null;
}

export interface RobotLifecycleItem {
  robot_id: string;
  name: string;
  state: RobotState | string;
  auth_mode: "per_robot" | "shared" | string;
  enrolled_at: string | null;
  credentials: RobotCredential[];
  open_codes: number;
}

export interface RobotLifecycleResponse {
  robots: RobotLifecycleItem[];
}

export interface CreateRobotRequest {
  robot_id: string;
  name?: string;
  metadata?: Record<string, unknown>;
}

export interface CreateRobotResponse {
  robot_id: string;
  state: RobotState | string;
  auth_mode: string;
  created_at: string;
}

export type MintPurpose = "enroll" | "reenroll";

export interface MintCodeRequest {
  ttl_seconds?: number;
  purpose?: MintPurpose;
  /**
   * Used only by the query service when it renders this one enrollment
   * command. It is deliberately not sent to ingest or persisted as robot
   * metadata, because it is a setting of this particular machine install.
   */
  recording_root?: string;
}

/**
 * Ingest's mint response plus the query service's rendered one-liner.
 *
 * `code` is "returned exactly once, never re-readable" — the plaintext exists
 * in this response and the operator's clipboard, nowhere else. It is therefore
 * never put in a react-query cache, a URL, or storage.
 *
 * `self_rotation_enabled` is OPTIONAL and normally absent: the mint
 * response does not carry it, and the ingest handler does not send it —
 * it is field 7 of the robot-facing gRPC `CredentialResponse`, a
 * deployment-wide switch the robot learns at enroll time. It is accepted here
 * so that a future proxy which chooses to surface the vault's setting is
 * displayed rather than dropped; it is never assumed present.
 */
export interface MintCodeResponse {
  code: string;
  code_id: string;
  fingerprint: string;
  expires_at: string;
  ca_spki_sha256: string;
  vault_ingest_endpoint: string;
  warning?: "robot_is_already_enrolled" | string;
  /** `reenroll` must use the installed agent, never the first-install bootstrap. */
  install_mode?: "install" | "reenroll" | string;
  install_command?: string;
  /** SHA-256 of the served `robot.sh`; null or absent means no command can check it. */
  robot_bootstrap_sha256?: string | null;
  recording_root?: string;
  self_rotation_enabled?: boolean;
}

export interface RobotStateChangeResponse {
  robot_id: string;
  /** The state actually WRITTEN. `enable` may land in `pending`. */
  state: RobotState | string;
  requested_state?: string;
  previous_state?: string;
}

export interface RevokeCredentialRequest {
  reason?: string;
  /** RFC 3339. `at <= now` is applied immediately, never rejected. */
  at?: string;
}

export interface RevokeCredentialResponse {
  credential_id: string;
  applied: "immediate" | "scheduled" | string;
  revoked: boolean;
  expires_at: string | null;
}

export type ProvisioningFailure =
  /** `require_role("admin")` refused, or there is no session at all. */
  | "forbidden"
  /** On any ingest error the proxy returns 502 `provisioning_unavailable`. */
  | "upstream_unavailable"
  | "conflict"
  | "rejected"
  | "unknown";

export class ProvisioningError extends Error {
  readonly reason: ProvisioningFailure;
  readonly httpStatus: number;
  readonly detail: unknown;

  constructor(reason: ProvisioningFailure, httpStatus: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ProvisioningError";
    this.reason = reason;
    this.httpStatus = httpStatus;
    this.detail = detail;
  }
}

/**
 * Map a transport failure onto something the page can say out loud.
 *
 * Every provisioning route is always mounted, so a 404 means the vault does not
 * know the robot (or credential, or code) named in the path.
 */
export function classifyProvisioningError(error: unknown): ProvisioningError {
  if (error instanceof ProvisioningError) return error;
  if (!(error instanceof HttpError)) {
    return new ProvisioningError("unknown", 0, "The vault did not answer.", undefined);
  }
  const { status, detail } = error;
  if (status === 404) {
    return new ProvisioningError(
      "rejected",
      status,
      "The vault does not know that robot, or that route.",
      detail
    );
  }
  if (status === 401 || status === 403) {
    return new ProvisioningError("forbidden", status, "Robot provisioning requires the admin role.", detail);
  }
  if (status === 502 || status === 503) {
    return new ProvisioningError(
      "upstream_unavailable",
      status,
      "The provisioning service did not answer. The vault did not synthesize a result.",
      detail
    );
  }
  if (status === 409 || status === 429) {
    return new ProvisioningError("conflict", status, conflictMessage(detail), detail);
  }
  if (status >= 400 && status < 500) {
    return new ProvisioningError("rejected", status, "The vault rejected that request.", detail);
  }
  return new ProvisioningError("unknown", status, `The vault answered HTTP ${status}.`, detail);
}

/**
 * Ingest's non-400 refusals are `{"error": "<code>"}` with no `detail` field
 * (`admin_v2.rs`), so without this an operator who hit ingest's "max 3
 * unconsumed, unexpired codes per robot" was told only "the vault refused
 * that change" -- true, useless, and not actionable.
 */
function conflictMessage(detail: unknown): string {
  switch (detail) {
    case "too_many_open_codes":
      return "This robot already has the maximum number of unused enrollment codes. Wait for one to expire, or revoke one first.";
    case "robot_decommissioned":
      return "This robot is decommissioned. Decommissioning is permanent. To use this hardware again, add it as a new robot.";
    case "robot_id_exists":
      return "A robot with that id already exists.";
    case "invalid_state_transition":
      return "The vault refused that state change as invalid from the robot's current state.";
    case "shared_credentials_denied":
      return "This vault refuses shared-credential robots.";
    default:
      return typeof detail === "string" && detail
        ? `The vault refused that change (${detail}).`
        : "The vault refused that change.";
  }
}

function encode(segment: string): string {
  return encodeURIComponent(segment);
}

export async function fetchRobotLifecycle(): Promise<RobotLifecycleResponse> {
  try {
    return await http<RobotLifecycleResponse>(ROBOTS_LIFECYCLE_PATH);
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

export async function createRobot(request: CreateRobotRequest): Promise<CreateRobotResponse> {
  try {
    return await http<CreateRobotResponse>(ROBOTS_CREATE_PATH, {
      method: "POST",
      body: JSON.stringify(request),
    });
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

export async function mintEnrollmentCode(
  robotId: string,
  request: MintCodeRequest = {}
): Promise<MintCodeResponse> {
  try {
    return await http<MintCodeResponse>(`/api/admin/robots/${encode(robotId)}/enrollment-code`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

/** `GET /api/admin/robots/bootstrap`: what the vault serves to robots, for the upgrade command. */
export interface RobotBootstrapResponse {
  agent_version: string;
  robot_bootstrap_sha256: string | null;
  /** Rendered by the server; null when it cannot render one that checks the script. */
  upgrade_command: string | null;
}

export async function fetchRobotBootstrap(): Promise<RobotBootstrapResponse> {
  try {
    return await http<RobotBootstrapResponse>("/api/admin/robots/bootstrap");
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

export type LifecycleAction = "disable" | "enable" | "decommission";

/**
 * Each action has its own route. Their ingest-side semantics are the part a
 * UI must not paraphrase:
 *
 * - `disable` is a reversible kill switch. Credentials are retained.
 * - `enable` **restores the state the robot was disabled FROM** — `enrolled`
 *   when `enrolled_at` is set, `pending` otherwise. The caller must render the
 *   `state` that comes back, never the one it asked for.
 * - `decommission` is terminal to the API and revokes every credential. The
 *   permanent: the hardware comes back only as a new robot.
 */
export async function changeRobotState(
  robotId: string,
  action: LifecycleAction,
  reason?: string
): Promise<RobotStateChangeResponse> {
  try {
    return await http<RobotStateChangeResponse>(`/api/admin/robots/${encode(robotId)}/${action}`, {
      method: "POST",
      body: JSON.stringify(reason ? { reason } : {}),
    });
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

export async function revokeCredential(
  robotId: string,
  credentialId: string,
  request: RevokeCredentialRequest = {}
): Promise<RevokeCredentialResponse> {
  try {
    return await http<RevokeCredentialResponse>(
      `/api/admin/robots/${encode(robotId)}/credentials/${encode(credentialId)}/revoke`,
      { method: "POST", body: JSON.stringify(request) }
    );
  } catch (error) {
    throw classifyProvisioningError(error);
  }
}

// ---------------------------------------------------------------------------
// Derived facts the list view needs
// ---------------------------------------------------------------------------

/**
 * Whether a credential can still authenticate.
 *
 * Mirrors ingest's own predicate (`enrollment.rs`):
 *
 *     revoked_at IS NULL
 *     AND (expires_at IS NULL OR expires_at > NOW())
 *     AND (superseded_at IS NULL OR superseded_grace_until > NOW())
 *
 * **`superseded_at` is deliberately NOT a short-circuit here.** During a
 * credential rotation, **both tokens authenticate during the window** —
 * default 24 h, configurable to 7 days — and the schema's
 * `robot_credentials_supersede_expiry` CHECK holds
 * `expires_at = superseded_grace_until` exactly. So the grace deadline IS on
 * the wire, as `expires_at`, which `GET /admin/v2/robots` serialises. Testing
 * `superseded_at` as well counted a mid-rotation robot's live credential as
 * dead: the operator read "0 live / 1" for a robot uploading right now, and
 * the UI hid Revoke on the one credential an incident response most needs to
 * reach.
 */
export function credentialIsLive(credential: RobotCredential, nowMs: number): boolean {
  if (credential.revoked_at) return false;
  if (credential.expires_at) {
    const expiry = Date.parse(credential.expires_at);
    if (Number.isFinite(expiry) && expiry <= nowMs) return false;
  }
  return true;
}

/**
 * Ingest's `ever_enrolled`, which decides whether a mint is a re-enrollment.
 *
 * Ingest computes it as `auth_mode == 'shared' || enrolled_at IS NOT NULL`
 * (`admin_v2.rs`). The `auth_mode` half is not redundant: the schema
 * defaults a robot to `('enrolled','shared')` with a NULL
 * `enrolled_at`, and `robots_enrolled_at_check` explicitly exempts
 * `auth_mode='shared'` so that row is legal and permanent. Testing only
 * `enrolled_at` would send `purpose: "enroll"` — and skip the required typed
 * re-enrollment confirmation — for every robot with `auth_mode='shared'`
 * and a NULL `enrolled_at`.
 */
export function hasEverEnrolled(
  robot: Pick<RobotLifecycleItem, "auth_mode" | "enrolled_at" | "state"> | null
): boolean {
  if (!robot) return false;
  return robot.auth_mode === "shared" || robot.enrolled_at !== null || robot.state === "enrolled";
}

/**
 * The one provisioning race that cannot be removed, made visible.
 *
 * If a mint commits and the response is lost in flight, the code is spent and
 * the robot has no token. The remedy is that this surfaces in the
 * UI as "enrolled, never uploaded" once the credential is more than ten
 * minutes old, and the operator's fix is one action: mint a replacement code.
 */
export const NEVER_USED_GRACE_MS = 10 * 60 * 1000;

export function isEnrolledButNeverUsed(credential: RobotCredential, nowMs: number): boolean {
  if (credential.first_used_at) return false;
  if (!credentialIsLive(credential, nowMs)) return false;
  const issued = Date.parse(credential.issued_at);
  if (!Number.isFinite(issued)) return false;
  return nowMs - issued > NEVER_USED_GRACE_MS;
}
