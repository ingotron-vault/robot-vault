import { useEffect, useState } from "react";
import { Button } from "@robot-vault/ui-kit";
import { useRobot, rootForRobotName } from "../context/RobotContext";
import { BASE_ROOT } from "../lib/api";
import { Chip } from "./Chip";

function robotNameFromRoot(root: string): string {
  const prefix = `${BASE_ROOT}/`;
  return root.startsWith(prefix) ? root.slice(prefix.length) : "";
}

export function StatusStrip() {
  const { root, setRoot, robots, robotsLoading, refetchRobots, status, statusStale } = useRobot();
  const [customRoot, setCustomRootInput] = useState(root);

  useEffect(() => {
    setCustomRootInput(root);
  }, [root]);

  const selectedRobotName = robotNameFromRoot(root);

  const serverReachable = status?.server_reachable ?? false;
  const hasStatus = !!status;

  const networkLabel = status
    ? `${(status.network_type || "unknown").toUpperCase()}${status.network_ssid ? ` - ${status.network_ssid}` : ""}`
    : "network unknown";

  return (
    <div id="robot-status" className={`mb-6 flex flex-col gap-3 rounded-lg border border-border bg-card px-4 py-3 ${statusStale ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Robot
          <select
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
            value={selectedRobotName}
            onChange={(e) => {
              if (e.target.value) setRoot(rootForRobotName(e.target.value));
            }}
          >
            <option value="">Select a robot...</option>
            {robots.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name} ({r.pending_count} pending)
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-1 min-w-[220px] flex-col gap-1 text-xs text-muted-foreground">
          Custom root
          <input
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
            placeholder={BASE_ROOT}
            value={customRoot}
            onChange={(e) => setCustomRootInput(e.target.value)}
            onBlur={() => {
              if (customRoot.trim()) setRoot(customRoot.trim());
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && customRoot.trim()) setRoot(customRoot.trim());
            }}
          />
        </label>

        <Button variant="secondary" size="sm" onClick={() => refetchRobots()} disabled={robotsLoading}>
          Refresh robots
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={hasStatus ? (serverReachable ? "success" : "danger") : "neutral"}>
          {hasStatus
            ? serverReachable
              ? `Server reachable (${status!.server_host}:${status!.server_port})`
              : `Server unreachable (${status!.server_host}:${status!.server_port})`
            : "Server status unknown"}
        </Chip>
        <Chip tone="neutral">{networkLabel}</Chip>
        {status ? <Chip tone="neutral">Connection: {status.connection_quality}</Chip> : null}
      </div>
    </div>
  );
}
