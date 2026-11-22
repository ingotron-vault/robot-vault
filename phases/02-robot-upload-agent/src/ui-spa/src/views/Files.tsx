import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Callout,
  EmptyState,
  ProgressBar,
  SegmentedControl,
  Skeleton,
  Table,
  TD,
  TH,
  THead,
  TR,
  useToast,
} from "@robot-vault/ui-kit";
import { useRobot } from "../context/RobotContext";
import { FileItem, UploadOptions, checkExists, getFiles, startUpload } from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import { NoRobotsEmptyState } from "../components/NoRobotsEmptyState";
import { useActivityLog } from "../lib/activityLog";
import { recordingStatus } from "../lib/recordingStatus";
import { RecordingDetails } from "../components/RecordingDetails";

type StatusFilter = "all" | "pending" | "uploading" | "completed" | "failed";

// `/api/files` uses the agent's canonical pending/uploading/completed/failed
// vocabulary. It never returns the old fifth spelling, `uploaded`; preserve
// that distinction so filters and manifest markers remain truthful.

interface MergedFile extends FileItem {
  bytes_sent?: number;
  total_bytes?: number;
}

const ROW_WINDOW = 120;

function FilesSkeleton() {
  return (
    <div className="space-y-3" aria-label="Loading recordings">
      <Skeleton className="h-7 w-56" />
      <Skeleton className="h-16" />
      <Skeleton className="h-72" />
    </div>
  );
}

export default function Files() {
  const { root, robots, robotsLoading, status, activeJob, activeJobId } = useRobot();
  const { toast } = useToast();
  const { log } = useActivityLog();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [visibleRows, setVisibleRows] = useState(ROW_WINDOW);
  const [showOptions, setShowOptions] = useState(false);
  const [bandwidthMbps, setBandwidthMbps] = useState(50);
  const [computeHashes, setComputeHashes] = useState(false);
  const filesQuery = useQuery({
    queryKey: ["files", root],
    queryFn: () => getFiles(root),
    enabled: !!root,
    refetchInterval: 30_000,
  });
  const previousJobId = useRef<string | undefined>();

  useEffect(() => {
    if (previousJobId.current && !activeJobId) {
      queryClient.invalidateQueries({ queryKey: ["files"] });
    }
    previousJobId.current = activeJobId;
  }, [activeJobId, queryClient]);

  useEffect(() => {
    setVisibleRows(ROW_WINDOW);
  }, [filter, root]);

  useEffect(() => {
    setSelected(new Set());
  }, [root]);

  const items = filesQuery.data ?? [];
  const activeByPath = new Map((activeJob?.files ?? []).map((file) => [file.path, file]));
  const merged: MergedFile[] = items.map((item) => {
    const live = activeByPath.get(item.path);
    return live
      ? {
          ...item,
          status: live.status,
          bytes_sent: live.bytes_sent,
          total_bytes: live.total_bytes,
          last_upload_error: live.error ?? item.last_upload_error,
        }
      : item;
  });
  const filtered = useMemo(
    () => merged.filter((file) => filter === "all" || file.status === filter),
    [filter, merged]
  );
  const counts = Object.fromEntries(
    (["all", "pending", "uploading", "completed", "failed"] as StatusFilter[]).map((value) => [
      value,
      value === "all" ? merged.length : merged.filter((file) => file.status === value).length,
    ])
  ) as Record<StatusFilter, number>;
  const pendingFiltered = filtered.filter((file) => file.status === "pending");
  const display = filtered.slice(0, visibleRows);
  const serverReachable = status?.server_reachable === true;
  const hasRunningJob = !!activeJob;
  const uploadActionTitle = !serverReachable
    ? "Ingest server unreachable"
    : hasRunningJob
      ? "An upload is already running for this robot"
      : "Starts one batch per click; repeat to drain remaining pending files.";

  const toggle = (path: string) => {
    setSelected((old) => {
      const next = new Set(old);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });
  };
  const selectPaths = (paths: string[]) => setSelected(new Set(paths));
  const clearFiltered = () => {
    const filteredPaths = new Set(filtered.map((file) => file.path));
    setSelected((old) => new Set([...old].filter((path) => !filteredPaths.has(path))));
  };

  const launch = useMutation({
    mutationFn: async (paths: string[]) => {
      // /api/exists proves only that the agent can see a local path. It is not
      // a remote-dedupe API, so completed state remains the local manifest's
      // honest record and this check is strictly a preflight availability test.
      const present = await checkExists(paths);
      if (present.missing.length) throw { kind: "missing", missing: present.missing };
      const options: UploadOptions = {
        bandwidth_mbps: bandwidthMbps,
        compute_chunk_hashes: computeHashes,
      };
      // There is no cancel endpoint: a started batch must run to a terminal
      // status, so the UI blocks a second active batch instead of implying a
      // cancellation or queue-reordering action that the agent cannot honor.
      return startUpload(root, paths, options);
    },
    onSuccess: (result) => {
      toast(`Upload started (job ${result.job_id.slice(0, 8)}).`, "info");
      log(`Upload job ${result.job_id} started for ${selected.size} file(s).`);
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (error: unknown) => {
      const missing = error && typeof error === "object" && (error as { kind?: string }).kind === "missing"
        ? (error as { missing: string[] }).missing
        : null;
      toast(
        missing
          ? `${missing.length} file(s) are not available to the agent. Set ROBOT_DATA_HOST_DIR and restart.`
          : "Upload failed to start.",
        "danger"
      );
      log(`Upload start failed: ${String(error)}`);
    },
  });

  if (!robotsLoading && !robots.length && !root) return <NoRobotsEmptyState />;

  if (root && filesQuery.isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Select local recordings</h1>
        </div>
        <FilesSkeleton />
      </div>
    );
  }

  if (root && filesQuery.isError) {
    return (
      <div className="flex flex-col gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Select local recordings</h1>
        </div>
        <EmptyState
          title="Could not load recordings"
          description="The agent did not return a file list for this root. No upload selection has been inferred."
          action={<Button size="sm" onClick={() => filesQuery.refetch()}>Retry file list</Button>}
        />
      </div>
    );
  }

  if (root && !items.length) {
    return (
      <EmptyState
        title="No recordings under this robot root"
        description={
          <>
            Place <code>.mcap</code> recordings below <code>{root}</code>. The agent only sees the host directory
            mounted as <code>ROBOT_DATA_HOST_DIR</code> below <code>BASE_ROOT</code>.
          </>
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-2xl font-semibold">Select local recordings</h1>
      </div>
      <Callout tone="info" title="Keep local recordings until server storage is checked">
        Upload recorded means the agent saved a successful upload. Verified at upload means the server checked that
        upload. Check current server storage and retention policy before removing local data.
      </Callout>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SegmentedControl
          value={filter}
          onChange={setFilter}
          label="Filter by upload status"
          options={(["all", "pending", "uploading", "completed", "failed"] as StatusFilter[]).map((value) => ({
            value,
            label: `${value} (${counts[value]})`,
          }))}
        />
        <span className="rounded-md border border-border bg-muted-foreground/5 px-3 py-1.5 text-sm text-muted-foreground">
          .mcap recordings only
        </span>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => selectPaths(filtered.map((file) => file.path))}>
            All filtered
          </Button>
          <Button variant="secondary" size="sm" onClick={clearFiltered}>
            None filtered
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => selectPaths(pendingFiltered.map((file) => file.path))}
          >
            Pending filtered
          </Button>
        </div>
      </div>
      <div className="max-h-[58vh] overflow-auto rounded-md border border-border">
        <Table>
          <THead>
            <TR>
              <TH style={{ width: 32 }} />
              <TH className="!text-foreground">Upload and verification</TH>
              <TH>Name</TH>
              <TH>Size</TH>
              <TH>Modified</TH>
              <TH className="!text-foreground">Local copy and next step</TH>
            </TR>
          </THead>
          <tbody>
            {display.map((item) => {
              const evidence = recordingStatus(item, { active: activeByPath.has(item.path), serverReachable });
              const progress = item.status === "uploading" && item.total_bytes
                ? (item.bytes_sent! / item.total_bytes) * 100
                : 0;
              return (
                <TR key={item.path}>
                  <TD>
                    <input
                      aria-label={`Select ${item.name}`}
                      type="checkbox"
                      checked={selected.has(item.path)}
                      onChange={() => toggle(item.path)}
                    />
                  </TD>
                  <TD className="min-w-44 align-top">
                    <p className="font-medium">{evidence.label}</p>
                    <p className="mt-1 text-xs text-foreground">{evidence.verification}</p>
                    {item.status === "uploading" ? (
                      <div className="mt-1 w-28">
                        <ProgressBar label={`Upload ${item.name} progress`} value={progress} />
                        <p className="mt-1 text-xs">This attempt</p>
                      </div>
                    ) : null}
                  </TD>
                  <TD className="min-w-40 align-top">
                    <p className="font-medium">{item.name}</p>
                    <p className="mt-1 max-w-xs break-all font-mono text-xs text-muted-foreground">{item.path}</p>
                  </TD>
                  <TD>{formatBytes(item.size)}</TD>
                  <TD className="whitespace-nowrap text-xs text-muted-foreground">{formatTimestamp(item.mtime)}</TD>
                  <TD className="min-w-60 max-w-sm align-top">
                    <p className="text-xs font-medium">{evidence.localPresent ? "On this robot" : "Local file missing"}</p>
                    <p className="mt-1 text-xs text-foreground">{evidence.nextAction}</p>
                    <RecordingDetails recording={item} path={item.path} />
                  </TD>
                </TR>
              );
            })}
            {!display.length ? (
              <TR>
                <TD colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                  No files match these filters.
                </TD>
              </TR>
            ) : null}
          </tbody>
        </Table>
        {filtered.length > display.length ? (
          <div className="border-t border-border p-2 text-center">
            <Button size="sm" variant="secondary" onClick={() => setVisibleRows((current) => current + ROW_WINDOW)}>
              Show {Math.min(ROW_WINDOW, filtered.length - display.length)} more
            </Button>
          </div>
        ) : null}
      </div>
      {!serverReachable ? (
        <p className="text-sm text-warning-foreground" role="status">
          Server connection unavailable. Local recordings remain visible. Check the connection before starting an upload.
        </p>
      ) : hasRunningJob ? (
        <p className="text-sm text-foreground" role="status">An upload is running for this robot. Follow its progress in the queue.</p>
      ) : null}
      <p className="text-xs text-foreground">Large uploads reuse parts the server has saved. Expired sessions and small uploads resend the whole file.</p>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-3">
        <span className="text-sm font-medium">{selected.size} file(s) selected</span>
        <Button
          disabled={!selected.size || !serverReachable || launch.isPending || hasRunningJob}
          title={uploadActionTitle}
          onClick={() => launch.mutate([...selected])}
        >
          Upload selected ({selected.size})
        </Button>
        <Button
          variant="secondary"
          disabled={!pendingFiltered.length || !serverReachable || launch.isPending || hasRunningJob}
          title={!pendingFiltered.length ? "No pending files match the current filter" : uploadActionTitle}
          onClick={() => launch.mutate(pendingFiltered.map((file) => file.path))}
        >
          Upload all pending ({pendingFiltered.length})
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setShowOptions((value) => !value)}>
          {showOptions ? "Hide options" : "Options"}
        </Button>
      </div>
      {showOptions ? (
        <div className="max-w-md rounded-lg border border-border bg-card p-4">
          <label className="flex items-center justify-between gap-2 text-sm">
            Bandwidth (Mbps)
            <input
              type="number"
              min={1}
              className="w-24 rounded-md border border-border bg-background px-2 py-1 text-sm"
              value={bandwidthMbps}
              onChange={(event) => setBandwidthMbps(Math.max(1, Math.floor(Number(event.target.value)) || 1))}
            />
          </label>
          <label className="mt-3 flex items-center justify-between gap-2 text-sm">
            Compute chunk hashes
            <input
              type="checkbox"
              checked={computeHashes}
              onChange={(event) => setComputeHashes(event.target.checked)}
            />
          </label>
        </div>
      ) : null}
    </div>
  );
}
