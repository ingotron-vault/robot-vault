import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  KpiTile,
  Skeleton,
  StatusPill,
  useToast,
} from "@robot-vault/ui-kit";
import { useRobot } from "../context/RobotContext";
import { getFiles, resumeAll, retryFailed } from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import { NoRobotsEmptyState } from "../components/NoRobotsEmptyState";
import { useActivityLog } from "../lib/activityLog";

interface RobotCardProps {
  name: string;
  path: string;
  pending: number;
  uploaded: number;
  failed: number;
  totalBytes: number;
  lastModified: number;
  selected: boolean;
  onSelect: () => void;
}

function RobotCard({
  name,
  path,
  pending,
  uploaded,
  failed,
  totalBytes,
  lastModified,
  selected,
  onSelect,
}: RobotCardProps) {
  const status = failed > 0 ? "failed" : pending > 0 ? "pending" : uploaded > 0 ? "completed" : "unknown";

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`rounded-lg border p-4 text-left transition-colors ${
        selected ? "border-accent bg-accent/10" : "border-border bg-card hover:bg-muted-foreground/5"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">{name}</span>
        <StatusPill status={status} />
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <span>
          <b>{pending}</b>
          <br />
          <span className="text-muted-foreground">pending</span>
        </span>
        <span>
          <b>{uploaded}</b>
          <br />
          <span className="text-muted-foreground">uploaded</span>
        </span>
        <span>
          <b>{failed}</b>
          <br />
          <span className="text-muted-foreground">failed</span>
        </span>
      </div>
      <div className="mt-3 text-xs text-muted-foreground">
        {formatBytes(totalBytes)} {" · "}last file {formatTimestamp(lastModified)}
      </div>
      <div className="mt-2 truncate font-mono text-[11px] text-muted-foreground">{path}</div>
    </button>
  );
}

export default function Dashboard() {
  const {
    root,
    setRoot,
    robots,
    robotsLoading,
    robotsError,
    refetchRobots,
    status,
    statusLoading,
    statusStale,
  } = useRobot();
  const { toast } = useToast();
  const { entries, log } = useActivityLog();
  const queryClient = useQueryClient();
  const files = useQuery({
    queryKey: ["dashboard-files", root],
    queryFn: () => getFiles(root),
    enabled: !!root,
    refetchInterval: 10_000,
  });
  const counts = (files.data ?? []).reduce(
    (value, file) => ({
      ...value,
      [file.status]: (value[file.status as keyof typeof value] || 0) + 1,
    }),
    { pending: 0, completed: 0, failed: 0, uploading: 0 } as Record<string, number>
  );
  const totalBytes = (files.data ?? []).reduce((sum, file) => sum + file.size, 0);
  const serverReachable = status?.server_reachable === true;
  const inventoryLoading = !!root && files.isLoading;
  const inventoryError = !!root && files.isError;

  const invalidate = () => {
    ["files", "dashboard-files", "jobs", "status"].forEach((key) => {
      queryClient.invalidateQueries({ queryKey: [key] });
    });
  };
  const resume = useMutation({
    mutationFn: () => resumeAll(root),
    onSuccess: (result) => {
      toast(
        `Upload: ${result.uploaded.length} uploaded, ${result.errors.length} failed.`,
        result.errors.length ? "warning" : "success"
      );
      log(`resume action finished: ${result.uploaded.length} uploaded, ${result.errors.length} failed.`);
      invalidate();
    },
    onError: (error) => {
      toast("Upload could not be started.", "danger");
      log(`resume action failed: ${String(error)}`);
    },
  });
  const retry = useMutation({
    mutationFn: () => retryFailed(root),
    onSuccess: (result) => {
      toast(
        `Retry: ${result.uploaded.length} uploaded, ${result.errors.length} failed.`,
        result.errors.length ? "warning" : "success"
      );
      log(`retry action finished: ${result.uploaded.length} uploaded, ${result.errors.length} failed.`);
      invalidate();
    },
    onError: (error) => {
      toast("Retry action failed.", "danger");
      log(`retry action failed: ${String(error)}`);
    },
  });

  if (robotsError && !root) {
    return (
      <EmptyState
        title="Could not discover robots"
        description="The robot agent did not return its discovery list. Check the local agent, then try again."
        action={<Button size="sm" onClick={() => refetchRobots()}>Retry discovery</Button>}
      />
    );
  }
  if (!robotsLoading && !robots.length && !root) return <NoRobotsEmptyState />;

  // No selected root means the status query is disabled. That is an unknown
  // state, not evidence that the ingest server is down.
  const agentLabel = statusLoading ? "Checking" : statusStale ? "Degraded" : status ? "Healthy" : "Unknown";
  const serverLabel = statusLoading
    ? "Checking"
    : statusStale || !status
      ? "Unknown"
      : status.server_reachable
        ? "Reachable"
        : "Unavailable";

  return (
    <div className="flex flex-col gap-6">
      <section className="rounded-xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-sm text-muted-foreground">Local upload operator console</p>
            <h1 className="mt-1 text-2xl font-semibold">
              {root ? root.split("/").pop() || "Selected robot" : "Choose a robot to start"}
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              The agent is loopback-only. It can browse local recordings even when the central ingest server is unreachable.
            </p>
          </div>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={!root || !serverReachable || resume.isPending}
              title="Starts one batch per click; repeat to drain remaining pending files."
              onClick={() => resume.mutate()}
            >
              Upload next batch
            </Button>
            <Button
              variant="secondary"
              disabled={!root || !serverReachable || retry.isPending}
              title="Retries all failed files under this root. Starts one batch per click; repeat to drain."
              onClick={() => retry.mutate()}
            >
              Retry failed
            </Button>
          </div>
        </div>
      </section>

      {statusLoading || inventoryLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((n) => <Skeleton key={n} className="h-28" />)}
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <KpiTile label="Agent" value={agentLabel} hint={statusStale ? "Last known status" : "Local API status"} />
          <KpiTile
            label="Ingest server"
            value={serverLabel}
            hint={status ? `${status.server_host}:${status.server_port}` : "Select a robot for a status check"}
          />
          <KpiTile
            label="Queue depth"
            value={root && !inventoryError ? counts.pending + counts.uploading : "—"}
            hint={inventoryError ? "Recording inventory unavailable" : root ? `${counts.uploading} uploading` : "No root selected"}
          />
          <KpiTile
            label="Local recordings"
            value={root && !inventoryError ? formatBytes(totalBytes) : "—"}
            hint={inventoryError ? "Recording inventory unavailable" : root ? `${counts.completed} completed · ${counts.failed} failed` : "Select a robot"}
          />
        </div>
      )}

      {inventoryError ? (
        <Callout tone="danger" title="Could not load the recording inventory">
          The agent did not return files for this root, so queue and storage totals are unknown.
          <Button className="ml-3" size="sm" variant="secondary" onClick={() => files.refetch()}>
            Retry inventory
          </Button>
        </Callout>
      ) : null}
      {status && !status.server_reachable && root ? (
        <Callout tone="warning" title="Central ingest is unavailable">
          You can inspect and select local files, but starting or retrying uploads is disabled until the agent can reach the configured ingest server.
        </Callout>
      ) : null}

      <section id="robot-discovery">
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold">Discovered robots</h2>
          <span className="text-sm text-muted-foreground">{robots.length} found</span>
        </div>
        {robotsLoading ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2].map((n) => <Skeleton key={n} className="h-40" />)}
          </div>
        ) : robotsError ? (
          <Callout tone="danger" title="Could not refresh discovered robots">
            The previous selection remains available, but the discovery list could not be refreshed.
            <Button className="ml-3" size="sm" variant="secondary" onClick={() => refetchRobots()}>
              Retry discovery
            </Button>
          </Callout>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {robots.map((robot) => (
              <RobotCard
                key={robot.path}
                name={robot.name}
                path={robot.path}
                pending={robot.pending_count}
                uploaded={robot.uploaded_count}
                failed={robot.failed_count}
                totalBytes={robot.total_size_bytes}
                lastModified={robot.last_modified}
                selected={root === robot.path}
                onSelect={() => setRoot(robot.path)}
              />
            ))}
          </div>
        )}
      </section>

      <Card>
        <CardHeader><CardTitle>Recent console activity</CardTitle></CardHeader>
        <CardBody>
          {entries.length ? (
            <ol className="space-y-2 text-sm">
              {entries.slice(-8).reverse().map((entry) => (
                <li key={entry.id} className="flex gap-3">
                  <time className="shrink-0 text-xs text-muted-foreground">
                    {new Date(entry.ts).toLocaleTimeString()}
                  </time>
                  <span>{entry.message}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">
              Start an upload, upload pending files, or retry a failure to see activity here.
            </p>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
