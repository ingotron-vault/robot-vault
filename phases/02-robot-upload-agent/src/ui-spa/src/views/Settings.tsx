import { Callout, Card, CardBody, CardHeader, CardTitle } from "@robot-vault/ui-kit";
import { formatTimestamp } from "../lib/format";
import { useActivityLog } from "../lib/activityLog";
import { useRobot } from "../context/RobotContext";

// The agent's own configuration (host/port, upload_agent limits, retention
// policy, TLS, throughput history, ...) is not exposed over HTTP anywhere in
// web.py -- the SPA's old /api/settings and /api/stats calls hit routes that
// never existed. Building that surface would be new backend work, not "wiring
// the SPA to its actual backend". The supported boundary is documented in the
// phase 02 technical guide. This view uses only the narrow, real status
// response (connection, concurrency, and retries) plus the client-side activity log;
// it must not imply that it can edit or inspect the complete live config.
export default function Settings() {
  const { entries, clear } = useActivityLog();
  const { status } = useRobot();

  return (
    <div className="flex flex-col gap-6">
      <Callout title="How configuration works">
        Configuration lives in <code>configs/vault.local.json</code> on the agent. Edit it and restart the agent to
        change it. The agent does not expose its live config or throughput history over HTTP; this console only shows
        connection, concurrency, and retry values from the status response.
      </Callout>

      <Card>
        <CardHeader><CardTitle>Effective connection status</CardTitle></CardHeader>
        <CardBody>
          {status ? (
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted-foreground">Ingest server</dt>
                <dd>
                  {status.server_host}:{status.server_port} {" · "}
                  {status.server_reachable ? "reachable" : "unreachable"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Network</dt>
                <dd>{status.network_type || "unknown"}{status.network_ssid ? ` · ${status.network_ssid}` : ""}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Parallel streams</dt>
                <dd>
                  {status.batch_strategy.parallel_streams} (retry multiplier {status.batch_strategy.retry_multiplier})
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-muted-foreground">
              Select a robot to show the current connection and upload policy.
            </p>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Activity log</CardTitle>
          <button className="text-xs text-muted-foreground hover:underline" onClick={clear}>
            Clear
          </button>
        </CardHeader>
        <CardBody>
          {entries.length === 0 ? (
            <div className="text-xs text-muted-foreground">No activity yet.</div>
          ) : (
            <div className="max-h-64 overflow-y-auto rounded-md bg-muted-foreground/5 p-2 font-mono text-xs">
              {entries
                .slice()
                .reverse()
                .map((e) => (
                  <div key={e.id}>
                    [{formatTimestamp(e.ts / 1000)}] {e.message}
                  </div>
                ))}
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
