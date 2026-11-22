/*
 * The one moment an enrollment code exists outside the vault.
 *
 * The rule: the plaintext code exists only in the mint response and the
 * operator's clipboard. The UI shows it **once**, with a "shown once" warning,
 * and cannot retrieve it afterwards. Everything in this component follows from
 * that rule -- the code is never written to a query cache, a URL, or
 * storage; it lives in the React state of the dialog that received it and dies
 * with it.
 */
import { useEffect, useState } from "react";
import { AlertTriangle, Clock } from "lucide-react";
import { Callout } from "@robot-vault/ui-kit";
import CopyableCommand from "../../components/CopyableCommand";
import {
  buildInstallCommand,
  buildReenrollmentCommand,
  expiryReadout,
  InstallCommandUnavailable,
} from "../../lib/enrollment";
import type { MintCodeResponse } from "../../lib/robotsAdmin";

export interface EnrollmentCodeProps {
  robotId: string;
  mint: MintCodeResponse;
  /** Injected in tests; production reads the browser's own address. */
  vaultUrl?: string;
  /** Injected in tests so the countdown is deterministic. */
  nowMs?: number;
}

interface CommandOutcome {
  command: string | null;
  problem: string | null;
  /** True when the vault rendered the command, false when we assembled it. */
  fromServer: boolean;
}

/**
 * Prefer the vault's own `install_command` and fall back to assembling it here.
 *
 * The fallback's `--vault` is this page's own origin, which is right whenever
 * the operator reached the vault at the address the robot will also use. It is
 * a guess in a way the server's `PUBLIC_BASE_URL` is not, so the UI says which
 * one it is showing. The fallback needs the server's script digest and fails
 * closed without it: no digest, no executable command.
 */
export function resolveInstallCommand(mint: MintCodeResponse, vaultUrl: string): CommandOutcome {
  if (typeof mint.install_command === "string" && mint.install_command.trim()) {
    return { command: mint.install_command, problem: null, fromServer: true };
  }
  try {
    const input = {
      vaultUrl,
      code: mint.code,
      sha256: mint.robot_bootstrap_sha256,
      ingestEndpoint: mint.vault_ingest_endpoint,
      recordingRoot: mint.recording_root,
    };
    return {
      command: mint.install_mode === "reenroll" ? buildReenrollmentCommand(input) : buildInstallCommand(input),
      problem: null,
      fromServer: false,
    };
  } catch (error) {
    const problem =
      error instanceof InstallCommandUnavailable
        ? error.message
        : "The install command could not be assembled.";
    return { command: null, problem, fromServer: false };
  }
}

export default function EnrollmentCode({ robotId, mint, vaultUrl, nowMs }: EnrollmentCodeProps) {
  // A local tick so the countdown rolls over without the fleet query polling
  // faster than it needs to. Frozen when the caller supplies `nowMs`.
  const [tick, setTick] = useState(() => nowMs ?? Date.now());
  useEffect(() => {
    if (nowMs !== undefined) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [nowMs]);

  const now = nowMs ?? tick;
  const expiry = expiryReadout(mint.expires_at, now);
  const origin = vaultUrl ?? window.location.origin;
  const install = resolveInstallCommand(mint, origin);
  const reenrolling = mint.install_mode === "reenroll";

  return (
    <div className="rv-robots-code">
      <Callout tone="warning" title="Shown once">
        This code is displayed here and nowhere else. The vault stores only its hash and
        cannot show it again — close this dialog without copying it and you will need to mint
        another.
      </Callout>

      {mint.warning === "robot_is_already_enrolled" ? (
        <Callout tone="warning" title="This robot is already enrolled">
          Redeeming this code issues a second credential for <strong>{robotId}</strong>. Use it
          only for rebuilt or re-imaged hardware.
        </Callout>
      ) : null}

      <dl className="rv-robots-code__facts">
        <div>
          <dt>Expires</dt>
          <dd data-tone={expiry.tone}>
            <Clock size={13} aria-hidden="true" />
            <span>{expiry.label}</span>
          </dd>
        </div>
        <div>
          <dt>Fingerprint</dt>
          <dd className="is-mono">{mint.fingerprint}</dd>
        </div>
        <div>
          <dt>Ingest endpoint</dt>
          <dd className="is-mono">{mint.vault_ingest_endpoint}</dd>
        </div>
        {typeof mint.self_rotation_enabled === "boolean" ? (
          <div>
            <dt>Self-rotation</dt>
            <dd>{mint.self_rotation_enabled ? "Enabled on this vault" : "Disabled on this vault"}</dd>
          </div>
        ) : null}
      </dl>

      {expiry.tone === "expired" ? (
        <div className="rv-robots-alert" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>
            This code has expired and will be refused. Mint a new one — the expired code is
            harmless and needs no cleanup.
          </span>
        </div>
      ) : null}

      <CopyableCommand label="Enrollment code" command={mint.code} />

      {install.command ? (
        <>
          <CopyableCommand
            label={reenrolling ? "Robot re-enrollment command" : "Robot install command"}
            command={install.command}
          />
          <p className="rv-robots-note">
            {install.fromServer
              ? "Rendered by the vault, which knows the address it publishes."
              : `Assembled from this browser's address (${origin}). Check it is the address the robot can reach.`}{" "}
            {reenrolling
              ? "This downloads the installer from the vault, checks its SHA-256, then replaces the robot's credentials. The installed agent, local recordings and queue state are kept."
              : "This downloads the installer from the vault and runs it only if its SHA-256 matches. The installer then checks the vault CA against the pin inside the code, so there is no fingerprint to paste separately."}
          </p>
        </>
      ) : (
        <div className="rv-robots-alert" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>
            {install.problem} The code above is still valid. Mint a new code once the vault reports its installer digest, or ask the vault administrator to check that the installer is available.
          </span>
        </div>
      )}

      <p className="rv-robots-note">
        Vault CA SPKI SHA-256: <code className="is-mono">{mint.ca_spki_sha256}</code>
      </p>
    </div>
  );
}
