import { useRobot } from "../context/RobotContext";

// Global edge-state banners, rendered on every view directly below the
// status strip:
//  - status fetch failing outright (SPA <-> agent) -> reconnect banner.
//  - agent reachable but its own status.server_reachable is false (agent <->
//    central ingest server) -> a distinct "local browsing available" banner.
export function Banners() {
  const { root, status, statusStale } = useRobot();

  if (!root) return null;

  return (
    <div className="mb-6 flex flex-col gap-2">
      {statusStale ? (
        <div className="w-full rounded-md border border-danger/30 bg-danger/10 px-4 py-2 text-sm text-danger">
          Could not reach the upload agent for the latest status. Showing the last known data.
        </div>
      ) : null}
      {status && !status.server_reachable ? (
        <div className="w-full rounded-md border border-warning/30 bg-warning/10 px-4 py-2 text-sm text-warning">
          Ingest server unreachable — local browsing available.
        </div>
      ) : null}
    </div>
  );
}
