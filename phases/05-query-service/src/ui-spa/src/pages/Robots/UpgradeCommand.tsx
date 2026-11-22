/*
 * The command that upgrades the agent on a robot that is already enrolled.
 *
 * The vault serves one agent version: its own. The command downloads the
 * installer from this vault, checks its SHA-256, and runs `--upgrade`. It
 * carries no enrollment code and keeps the robot's CA, config and credential.
 */
import { useQuery } from "@tanstack/react-query";
import { Callout } from "@robot-vault/ui-kit";
import CopyableCommand from "../../components/CopyableCommand";
import { buildUpgradeCommand, InstallCommandUnavailable } from "../../lib/enrollment";
import {
  fetchRobotBootstrap,
  type ProvisioningError,
  type RobotBootstrapResponse,
} from "../../lib/robotsAdmin";

export interface UpgradeOutcome {
  command: string | null;
  problem: string | null;
  fromServer: boolean;
}

/** The server's `upgrade_command` verbatim, else assembled from its digest and this page's address. */
export function resolveUpgradeCommand(info: RobotBootstrapResponse, vaultUrl: string): UpgradeOutcome {
  if (typeof info.upgrade_command === "string" && info.upgrade_command.trim()) {
    return { command: info.upgrade_command, problem: null, fromServer: true };
  }
  try {
    return {
      command: buildUpgradeCommand({ vaultUrl, sha256: info.robot_bootstrap_sha256 }),
      problem: null,
      fromServer: false,
    };
  } catch (error) {
    const problem =
      error instanceof InstallCommandUnavailable ? error.message : "The upgrade command could not be assembled.";
    return { command: null, problem, fromServer: false };
  }
}

export default function UpgradeCommand({ vaultUrl }: { vaultUrl?: string }) {
  const bootstrapQ = useQuery<RobotBootstrapResponse, ProvisioningError>({
    queryKey: ["robots-bootstrap"],
    queryFn: fetchRobotBootstrap,
    retry: false,
  });
  const origin = vaultUrl ?? window.location.origin;
  const outcome = bootstrapQ.data ? resolveUpgradeCommand(bootstrapQ.data, origin) : null;

  return (
    <section id="robots-upgrade" className="rv-robots-panel" aria-labelledby="rv-robots-upgrade-title">
      <div className="rv-robots-panel__head">
        <div>
          <h2 id="rv-robots-upgrade-title">Upgrade the agent</h2>
          <p className="rv-robots-panel__meta">
            {bootstrapQ.data
              ? `This vault serves agent version ${bootstrapQ.data.agent_version}.`
              : "Run on a robot that is already enrolled."}
          </p>
        </div>
      </div>
      {bootstrapQ.isLoading ? <p className="rv-robots-note">Loading the upgrade command…</p> : null}
      {bootstrapQ.isError ? (
        <div className="rv-robots-alert" role="alert">
          <span>{bootstrapQ.error.message}</span>
        </div>
      ) : null}
      {outcome?.command ? (
        <>
          <CopyableCommand label="Robot upgrade command" command={outcome.command} />
          <p className="rv-robots-note">
            {outcome.fromServer
              ? "Rendered by the vault, which knows the address it publishes."
              : `Assembled from this browser's address (${origin}). Check it is the address the robot can reach.`}{" "}
            The robot runs the installer only if its SHA-256 matches. An older version is refused, and the same version
            changes nothing. The robot keeps its credential and recordings.
          </p>
        </>
      ) : null}
      {outcome && !outcome.command ? (
        <Callout tone="warning" title="No upgrade command">
          {outcome.problem}
        </Callout>
      ) : null}
    </section>
  );
}
