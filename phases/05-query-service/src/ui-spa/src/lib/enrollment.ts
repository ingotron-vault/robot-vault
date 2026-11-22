/*
 * Enrollment codes and the command an operator pastes onto a robot.
 *
 * THE VAULT SERVES ITS OWN INSTALLER
 * ==================================
 * The robot downloads `robot.sh` from this vault (`GET /robot`), checks it
 * against the SHA-256 in the command, and only then runs it:
 *
 *     f=$(mktemp) && curl -fsSk --proto =https https://vault.example.com/robot -o "$f" \
 *       && echo "<sha256>  $f" | sha256sum -c - \
 *       && sudo sh "$f" --vault https://vault.example.com --ingest ingest.example.com:50051 --code ign1.…
 *
 * It is not `curl … | sh`: a pipe cannot check a digest, and `-k` is safe only
 * because nothing runs before the check passes. The digest reaches the admin
 * over their authenticated HTTPS session; the script carries the agent digest;
 * the code carries the CA pin.
 *
 * WHEN THE SERVER RENDERS IT, THE SERVER WINS
 * -------------------------------------------
 * The query service returns `install_command` with the mint, built from its
 * `PUBLIC_BASE_URL`. A browser only knows the address the operator typed, so
 * `buildInstallCommand` is the fallback. It fails closed: with no valid
 * digest (`robot_bootstrap_sha256`) it renders nothing executable.
 */

/** Enrollment code format: `ign1.<26-char CA pin>.<32-char secret>`, 64 characters total. */
export const ENROLLMENT_CODE_PREFIX = "ign1.";
export const ENROLLMENT_PIN_LENGTH = 26;
export const ENROLLMENT_SECRET_LENGTH = 32;
export const ENROLLMENT_CODE_LENGTH =
  ENROLLMENT_CODE_PREFIX.length + ENROLLMENT_PIN_LENGTH + 1 + ENROLLMENT_SECRET_LENGTH;

/** The robot installer uses this when `--root` is omitted. */
export const DEFAULT_RECORDING_ROOT = "/data/robot_data";

/** Keep browser-side command generation aligned with the query-service contract. */
export const MAX_RECORDING_ROOT_LENGTH = 4096;

/** The robot lifecycle states: the `robots_state_check` `CHECK (state IN (...))`. */
export const ROBOT_STATES = ["pending", "enrolled", "disabled", "decommissioned"] as const;
export type RobotState = (typeof ROBOT_STATES)[number];

export function isKnownRobotState(value: string): value is RobotState {
  return (ROBOT_STATES as readonly string[]).includes(value);
}

/**
 * Crockford's base32 alphabet, uppercase only.
 *
 * The vault does not case-fold and does not repair `I`->`1` or `O`->`0`
 * (`crockford.rs`), so neither does this. A code is opaque and copy-pasted.
 */
const CROCKFORD = /^[0-9A-HJKMNP-TV-Z]+$/;

export interface ParsedEnrollmentCode {
  pin: string;
  secret: string;
}

/** The shape check `robot.sh` performs before any network call. */
export function parseEnrollmentCode(code: string): ParsedEnrollmentCode | null {
  if (typeof code !== "string") return null;
  if (!code.startsWith(ENROLLMENT_CODE_PREFIX)) return null;
  const parts = code.slice(ENROLLMENT_CODE_PREFIX.length).split(".");
  if (parts.length !== 2) return null;
  const [pin, secret] = parts;
  if (pin.length !== ENROLLMENT_PIN_LENGTH || secret.length !== ENROLLMENT_SECRET_LENGTH) return null;
  if (!CROCKFORD.test(pin) || !CROCKFORD.test(secret)) return null;
  return { pin, secret };
}

export type InstallCommandFailure =
  | "invalid_vault_url"
  | "invalid_code"
  | "invalid_recording_root"
  | "invalid_digest"
  | "invalid_ingest_endpoint";

export class InstallCommandUnavailable extends Error {
  readonly reason: InstallCommandFailure;

  constructor(reason: InstallCommandFailure, message: string) {
    super(message);
    this.name = "InstallCommandUnavailable";
    this.reason = reason;
  }
}

/** SHA-256 of the served `robot.sh`, as the server reports it: 64 lowercase hex digits. */
export function normalizeBootstrapDigest(candidate: unknown): string {
  if (typeof candidate !== "string" || !/^[0-9a-f]{64}$/.test(candidate)) {
    throw new InstallCommandUnavailable(
      "invalid_digest",
      "The vault did not report a valid SHA-256 for its installer script, so no command can be shown that checks it."
    );
  }
  return candidate;
}

/** `host:port` (DNS name, IPv4 or `[IPv6]`), the form `--ingest` takes. */
export function normalizeIngestEndpoint(candidate: unknown): string {
  const match =
    typeof candidate === "string"
      ? /^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]):([0-9]{1,5})$/.exec(candidate)
      : null;
  const port = match ? Number(match[1]) : 0;
  if (!match || port < 1 || port > 65535) {
    throw new InstallCommandUnavailable(
      "invalid_ingest_endpoint",
      "The ingest endpoint is not a host:port address."
    );
  }
  return candidate as string;
}

/**
 * `https://host[:port]` with nothing after the authority.
 *
 * `robot.sh` requires `--vault` to parse as `https://host[:port]`, and
 * then fetches `"$VAULT/ca.crt"` — so a trailing slash, a path, a query
 * or a fragment would produce a URL `robot.sh` cannot use. Rejecting is the
 * only honest answer: emitting a command that fails on the robot, minutes
 * later and with a stranger holding the terminal, is worse than saying so here.
 */
export function normalizeVaultUrl(candidate: string): string {
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new InstallCommandUnavailable("invalid_vault_url", "The vault address is not a URL.");
  }
  if (url.protocol !== "https:") {
    throw new InstallCommandUnavailable(
      "invalid_vault_url",
      "The robot installer requires an https vault address."
    );
  }
  if (url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== "/")) {
    throw new InstallCommandUnavailable(
      "invalid_vault_url",
      "The vault address must be https://host[:port] with no path."
    );
  }
  return url.origin;
}

/**
 * The subset of the robot installer's `--root` preflight that a browser can
 * check before it prints a command. The installer repeats these checks on the
 * robot, where it also checks the actual filesystem permissions.
 *
 * Keep the value boring. It is placed in a generated shell command and later
 * reaches a systemd drop-in, so reject characters that either consumer treats
 * specially instead of trying to escape around a surprising directory name.
 */
export function normalizeRecordingRoot(candidate: string): string {
  const root = candidate;
  if (!root) {
    throw new InstallCommandUnavailable("invalid_recording_root", "A recording directory is required.");
  }
  // The query-service validates this before minting a code. Count Unicode code
  // points like Python's `len`, rather than JS UTF-16 code units, so both sides
  // make the same decision for non-ASCII path names.
  if (Array.from(root).length > MAX_RECORDING_ROOT_LENGTH) {
    throw new InstallCommandUnavailable(
      "invalid_recording_root",
      `The recording directory must be ${MAX_RECORDING_ROOT_LENGTH} characters or fewer.`
    );
  }
  if (!root.startsWith("/")) {
    throw new InstallCommandUnavailable(
      "invalid_recording_root",
      "The recording directory must be an absolute path."
    );
  }
  if (root === "/" || root.endsWith("/") || root.includes("//") || root.split("/").includes("..")) {
    throw new InstallCommandUnavailable(
      "invalid_recording_root",
      "The recording directory must be a normalised absolute path outside the filesystem root."
    );
  }
  if (/[\u0000-\u0020\u007f'"$`\\%]/.test(root)) {
    throw new InstallCommandUnavailable(
      "invalid_recording_root",
      "The recording directory may not contain ASCII whitespace, control characters, or shell/systemd metacharacters."
    );
  }
  const denied = [
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/lib/ingotron",
    "/opt/ingotron-agent",
  ];
  if (denied.some((prefix) => root === prefix || root.startsWith(`${prefix}/`))) {
    throw new InstallCommandUnavailable(
      "invalid_recording_root",
      "The recording directory cannot be inside a system-owned path."
    );
  }
  return root;
}

function shellQuote(value: string): string {
  // Every substituted value is validated first; quoting is the second lock.
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : `'${value.replace(/'/g, `'\\''`)}'`;
}

/**
 * The download, check, run command. `args` are the robot.sh arguments after
 * the script path. No trailing newline: a pasted newline would run it before
 * the operator has read it.
 */
function renderBootstrapCommand(vaultUrl: string, digest: string, args: string[]): string {
  const vault = normalizeVaultUrl(vaultUrl);
  const sha = normalizeBootstrapDigest(digest);
  return [
    `f=$(mktemp) && curl -fsSk --proto =https ${shellQuote(`${vault}/robot`)} -o "$f"`,
    `&& echo "${sha}  $f" | sha256sum -c -`,
    `&& sudo sh "$f" ${args.map(shellQuote).join(" ")}`,
  ].join(" \\\n  ");
}

interface EnrollCommandInput {
  vaultUrl: string;
  code: string;
  /** `robot_bootstrap_sha256` from the mint response. */
  sha256: string | null | undefined;
  /** `vault_ingest_endpoint` from the mint response. */
  ingestEndpoint: string;
  recordingRoot?: string;
}

function enrollArgs(input: EnrollCommandInput, lead: string[]): string[] {
  if (!parseEnrollmentCode(input.code)) {
    throw new InstallCommandUnavailable("invalid_code", "That is not a well-formed enrollment code.");
  }
  const root = input.recordingRoot === undefined ? [] : ["--root", normalizeRecordingRoot(input.recordingRoot)];
  return [
    ...lead,
    "--vault",
    normalizeVaultUrl(input.vaultUrl),
    "--ingest",
    normalizeIngestEndpoint(input.ingestEndpoint),
    "--code",
    input.code,
    ...root,
  ];
}

/** First install, assembled in the browser. Prefer the server's `install_command`. */
export function buildInstallCommand(input: EnrollCommandInput): string {
  const args = enrollArgs(input, []);
  return renderBootstrapCommand(input.vaultUrl, input.sha256 as string, args);
}

/** A robot that already has the agent: replace its credentials, keep the agent and its data. */
export function buildReenrollmentCommand(input: EnrollCommandInput): string {
  const args = enrollArgs(input, ["--reenroll"]);
  return renderBootstrapCommand(input.vaultUrl, input.sha256 as string, args);
}

/** Upgrade the agent from this vault. No code: the vault's own bytes, anchored by the digest. */
export function buildUpgradeCommand(input: { vaultUrl: string; sha256: string | null | undefined }): string {
  return renderBootstrapCommand(input.vaultUrl, input.sha256 as string, [
    "--upgrade",
    "--vault",
    normalizeVaultUrl(input.vaultUrl),
  ]);
}

// ---------------------------------------------------------------------------
// The 15-minute clock
// ---------------------------------------------------------------------------

export type ExpiryTone = "fresh" | "expiring" | "expired" | "unknown";

export interface ExpiryReadout {
  tone: ExpiryTone;
  /** Whole seconds left, or `null` when `expires_at` was unreadable. */
  remainingSeconds: number | null;
  /** `12:03 left`, `Expired`, or `Expiry unknown`. */
  label: string;
}

/**
 * How long a minted code has left.
 *
 * The vault evaluates expiry in SQL against its own clock; this readout is
 * a convenience for the operator's eyes and says nothing the server relies on.
 * An `expires_at` we cannot parse becomes `unknown` rather than `expired`: a
 * code that is actually live must not be presented as dead, because the
 * operator's recovery from "dead" is to mint another one and burn this one.
 */
export function expiryReadout(expiresAt: string | null | undefined, nowMs: number): ExpiryReadout {
  if (!expiresAt) return { tone: "unknown", remainingSeconds: null, label: "Expiry unknown" };
  const deadline = Date.parse(expiresAt);
  if (!Number.isFinite(deadline)) {
    return { tone: "unknown", remainingSeconds: null, label: "Expiry unknown" };
  }
  const remaining = Math.floor((deadline - nowMs) / 1000);
  if (remaining <= 0) return { tone: "expired", remainingSeconds: 0, label: "Expired" };
  return {
    // 120 s is where an operator still has time to walk to the robot but should
    // stop reading and start pasting.
    tone: remaining <= 120 ? "expiring" : "fresh",
    remainingSeconds: remaining,
    label: `${formatClock(remaining)} left`,
  };
}

/** `mm:ss`, or `h:mm:ss` past an hour (a 24 h TTL is legal). */
export function formatClock(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}
