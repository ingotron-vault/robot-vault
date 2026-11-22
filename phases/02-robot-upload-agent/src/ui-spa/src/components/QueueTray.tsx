import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, ProgressBar, StatusPill, useToast } from "@robot-vault/ui-kit";
import { retryFailed, type JobFile } from "../lib/api";
import { useRobot } from "../context/RobotContext";
import { formatBytes, formatPercent } from "../lib/format";
import { useActivityLog } from "../lib/activityLog";
import { uploadRecovery } from "../lib/recordingStatus";

function errorText(error: Record<string, unknown> | null): string {
  if (!error) return "Upload failed without a reason from the agent.";
  const detail = error.detail;
  return typeof detail === "string" ? detail : JSON.stringify(error);
}

function FileProgress({ file }: { file: JobFile }) {
  const percent = file.total_bytes ? (file.bytes_sent / file.total_bytes) * 100 : 0;

  return (
    <li className="rounded-md border border-border/70 p-2">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="min-w-0 truncate font-medium" title={file.path}>
          {file.path.split("/").pop()}
        </span>
        <StatusPill status={file.status} />
      </div>
      <div className="mt-2 flex items-center gap-2">
        <ProgressBar
          label={`Upload ${file.path} progress`}
          value={percent}
          tone={file.status === "failed" ? "danger" : "info"}
        />
        <span className="shrink-0 text-xs text-foreground">
          {formatBytes(file.bytes_sent)} / {formatBytes(file.total_bytes)} this attempt
        </span>
      </div>
      {file.status === "failed" ? (
        <><p className="mt-1 break-words text-xs text-danger-foreground">
          <span className="font-medium">Failure reason:</span> {errorText(file.error)}
        </p>
        <p className="mt-1 text-xs text-foreground">{uploadRecovery(file.error)} Retry reuses saved parts when supported.</p></>
      ) : null}
      {file.status === "completed" ? <p className="mt-1 text-xs text-foreground">Upload recorded. Check History for verification before removing local data.</p> : null}
    </li>
  );
}

/**
 * Persistent, API-backed live queue. It intentionally has no cancel action:
 * the robot agent has no cancel endpoint. Completed jobs remain inspectable in
 * History, including any per-file failure reason the in-memory job retains.
 */
export function QueueTray() {
  const { root, activeJob, status } = useRobot();
  const { toast } = useToast();
  const { log } = useActivityLog();
  const queryClient = useQueryClient();
  const [bytesPerSecond, setBytesPerSecond] = useState<number | null>(null);
  const previous = useRef<{ bytes: number; at: number } | null>(null);
  const totalSent = activeJob?.files.reduce((sum, file) => sum + file.bytes_sent, 0) ?? 0;
  const totalBytes = activeJob?.files.reduce((sum, file) => sum + file.total_bytes, 0) ?? 0;
  const failed = activeJob?.files.filter((file) => file.status === "failed") ?? [];

  useEffect(() => {
    if (!activeJob) {
      previous.current = null;
      setBytesPerSecond(null);
      return;
    }

    const now = Date.now();
    if (previous.current && now > previous.current.at && totalSent >= previous.current.bytes) {
      setBytesPerSecond(((totalSent - previous.current.bytes) * 1000) / (now - previous.current.at));
    }
    previous.current = { bytes: totalSent, at: now };
  }, [activeJob, totalSent]);

  const retry = useMutation({
    mutationFn: () => retryFailed(root),
    onSuccess: (result) => {
      toast(
        `Retry failed: ${result.uploaded.length} uploaded, ${result.errors.length} still failed.`,
        result.errors.length ? "warning" : "success"
      );
      log(`Queue tray retried failed uploads: ${result.uploaded.length} uploaded, ${result.errors.length} failed.`);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (error) => {
      toast("Retry failed could not be started.", "danger");
      log(`Queue tray retry failed: ${String(error)}`);
    },
  });

  if (!activeJob) {
    return (
      <aside
        id="queue-tray"
        aria-label="Upload queue"
        className="border-t border-border bg-card"
      >
        <div className="mx-auto max-w-7xl px-4 py-3 text-sm text-muted-foreground">
          <span className="font-medium text-foreground">Queue tray</span>
          {" · "}No active uploads. Start a selected batch from Files.
        </div>
      </aside>
    );
  }

  const percent = totalBytes ? (totalSent / totalBytes) * 100 : 0;

  return (
    <aside
      id="queue-tray"
      aria-label="Upload queue"
      className="border-t border-border bg-card"
    >
      <div className="mx-auto max-w-7xl px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <span>Queue tray</span>
              <StatusPill status={activeJob.status} />
            </div>
            <div className="text-xs text-foreground">
              {activeJob.files.length} file(s) {" · "}
              {formatBytes(totalSent)} / {formatBytes(totalBytes)} this attempt {" · "}
              {formatPercent(percent)}
              {bytesPerSecond != null ? ` · ${formatBytes(bytesPerSecond)}/s` : ""}
            </div>
          </div>
          <div className="min-w-[12rem] flex-1">
            <ProgressBar
              label="Upload queue progress"
              value={percent}
              tone={failed.length ? "danger" : "info"}
            />
          </div>
          {failed.length ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={!root || !status?.server_reachable || retry.isPending}
              onClick={() => retry.mutate()}
            >
              Retry failed under this root
            </Button>
          ) : null}
        </div>
        <ul className="mt-3 grid max-h-48 gap-2 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">
          {activeJob.files.map((file) => <FileProgress key={file.path} file={file} />)}
        </ul>
      </div>
    </aside>
  );
}
