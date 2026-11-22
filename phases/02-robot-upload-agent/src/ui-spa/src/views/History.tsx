import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  Skeleton,
  StatusPill,
  Table,
  TD,
  TH,
  THead,
  TR,
} from "@robot-vault/ui-kit";
import { useRobot } from "../context/RobotContext";
import { HistoryEntry, JobFile, getHistory, getJobs } from "../lib/api";
import { formatBytes, formatIsoTimestamp, formatTimestamp } from "../lib/format";
import { recordingStatus, uploadRecovery } from "../lib/recordingStatus";
import { RecordingDetails } from "../components/RecordingDetails";

function dayFor(entry: HistoryEntry) {
  const raw = entry.recorded_at ?? entry.created_at;
  const date = raw ? new Date(raw) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" })
    : "Unknown date";
}

function errorText(error: Record<string, unknown> | null): string {
  if (!error) return "Upload failed without a reason from the agent.";
  const detail = error.detail;
  return typeof detail === "string" ? detail : JSON.stringify(error);
}

function JobFileDetails({ file }: { file: JobFile }) {
  return (
    <li className="rounded-md border border-border/70 p-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <code className="min-w-0 break-all text-xs">{file.path}</code>
        <StatusPill status={file.status} />
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {formatBytes(file.bytes_sent)} / {formatBytes(file.total_bytes)} sent this attempt
      </p>
      {file.status === "failed" ? (
        <>
          <p className="mt-1 break-words text-xs text-danger-foreground">
            <span className="font-medium">Failure reason:</span> {errorText(file.error)}
          </p>
          <p className="mt-1 text-xs text-foreground">{uploadRecovery(file.error)} Retry reuses saved parts when supported.</p>
        </>
      ) : null}
      {file.status === "completed" ? <p className="mt-1 text-xs text-foreground">Upload recorded. Check the recording history below for verification evidence.</p> : null}
    </li>
  );
}

function HistorySkeleton() {
  return (
    <Card aria-label="Loading upload history">
      <CardHeader><CardTitle>Loading upload history</CardTitle></CardHeader>
      <CardBody className="space-y-3">
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-4/5" />
      </CardBody>
    </Card>
  );
}

export default function History() {
  const { root } = useRobot();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  // Live jobs are the process-local queue and retain per-file errors. Durable
  // history is manifest-derived and survives restarts, but does not invent an
  // error payload once that in-memory job has gone away.
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: getJobs,
    refetchInterval: 5000,
  });
  const history = useQuery({
    queryKey: ["history", root],
    queryFn: () => getHistory(root || undefined),
    refetchInterval: 30_000,
  });
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (history.data?.files ?? []).filter((entry) => {
      return !needle || `${entry.path} ${entry.status} ${entry.upload_id ?? ""}`.toLowerCase().includes(needle);
    });
  }, [history.data?.files, search]);
  const toggle = (path: string) => {
    setExpanded((old) => {
      const next = new Set(old);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });
  };
  const toggleJob = (jobId: string) => {
    setExpandedJobs((old) => {
      const next = new Set(old);
      next.has(jobId) ? next.delete(jobId) : next.add(jobId);
      return next;
    });
  };
  let lastDay = "";

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Upload history</h1>
        <p className="mt-2 text-sm text-foreground">Saved on this robot and retained after restart. Upload receipts describe verification at upload time. Check current server storage and retention policy before deleting local recordings.</p>
      </div>
      <label className="max-w-md text-sm font-medium">
        Search history
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Path, outcome, or upload ID"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-normal"
        />
      </label>

      <Card>
        <CardHeader><CardTitle>Live jobs retained by this agent process</CardTitle></CardHeader>
        {jobs.isLoading ? (
          <CardBody className="space-y-2">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-4/5" />
          </CardBody>
        ) : jobs.isError ? (
          <CardBody>
            <Callout tone="danger" title="Could not load live jobs">
              The agent did not return its in-memory job list. Durable manifest history below may still be available.
              <Button className="ml-3" size="sm" variant="secondary" onClick={() => jobs.refetch()}>
                Retry live jobs
              </Button>
            </Callout>
          </CardBody>
        ) : !(jobs.data ?? []).length ? (
          <CardBody>
            <p className="text-sm text-muted-foreground">No upload jobs are currently retained by this process.</p>
          </CardBody>
        ) : (
          <div className="overflow-auto">
          <Table>
            <THead>
              <TR>
                <TH style={{ width: 28 }} />
                <TH>Started</TH>
                <TH>Status</TH>
                <TH>Files</TH>
                <TH>Uploaded</TH>
                <TH>Failed</TH>
              </TR>
            </THead>
            <tbody>
              {(jobs.data ?? [])
                .slice()
                .sort((a, b) => b.started_at - a.started_at)
                .map((job) => {
                  const open = expandedJobs.has(job.job_id);
                  return (
                    <Fragment key={job.job_id}>
                      <TR>
                        <TD>
                          <button
                            type="button"
                            aria-label={`Toggle files for job ${job.job_id}`}
                            aria-expanded={open}
                            onClick={() => toggleJob(job.job_id)}
                          >
                            {open ? "▾" : "▸"}
                          </button>
                        </TD>
                        <TD>{formatTimestamp(job.started_at)}</TD>
                        <TD><StatusPill status={job.status} /></TD>
                        <TD>{job.files.length}</TD>
                        <TD>{job.files.filter((file) => file.status === "completed").length}</TD>
                        <TD>{job.files.filter((file) => file.status === "failed").length}</TD>
                      </TR>
                      {open ? (
                        <TR>
                          <TD colSpan={6} className="bg-muted-foreground/5">
                            <ul className="grid gap-2 p-2 sm:grid-cols-2">
                              {job.files.map((file) => <JobFileDetails key={file.path} file={file} />)}
                            </ul>
                          </TD>
                        </TR>
                      ) : null}
                    </Fragment>
                  );
                })}
            </tbody>
          </Table>
          </div>
        )}
      </Card>

      {history.isLoading ? (
        <HistorySkeleton />
      ) : history.isError ? (
        <EmptyState
          title="Could not load durable upload history"
          description="The agent did not return local manifests, so no history outcome has been inferred."
          action={<Button size="sm" onClick={() => history.refetch()}>Retry history</Button>}
        />
      ) : !filtered.length ? (
        <EmptyState
          title="No matching upload history"
          description={root ? "The selected robot has no matching durable manifests yet." : "Select a robot to scope the durable history."}
        />
      ) : (
        <Card>
          <CardHeader><CardTitle>Upload history{root ? ` — ${root}` : ""}</CardTitle></CardHeader>
          <div className="overflow-auto">
            <Table>
              <THead>
                <TR>
                  <TH style={{ width: 28 }} />
                  <TH>Recorded</TH>
                  <TH className="!text-foreground">Upload and verification</TH>
                  <TH>Size</TH>
                  <TH>File</TH>
                  <TH className="!text-foreground">Local copy and next step</TH>
                </TR>
              </THead>
              <tbody>
                {filtered.map((entry) => {
                  const evidence = recordingStatus(entry, {
                    active: jobs.data?.some((job) => job.status === "running" && job.files.some((file) => file.path === entry.path)),
                  });
                  const day = dayFor(entry);
                  const showDay = day !== lastDay;
                  lastDay = day;
                  const open = expanded.has(entry.path);
                  return (
                    <Fragment key={entry.path}>
                      {showDay ? (
                        <TR>
                          <TH colSpan={6} className="bg-muted-foreground/5 px-3 py-2 text-left text-xs font-semibold">
                            {day}
                          </TH>
                        </TR>
                      ) : null}
                      <TR>
                        <TD>
                          <button
                            type="button"
                            onClick={() => toggle(entry.path)}
                            aria-label={`Toggle manifest details for ${entry.path}`}
                            aria-expanded={open}
                          >
                            {open ? "▾" : "▸"}
                          </button>
                        </TD>
                        <TD>{formatIsoTimestamp(entry.recorded_at ?? entry.created_at)}</TD>
                        <TD className="min-w-44 align-top">
                          <p className="font-medium">{evidence.label}</p>
                          <p className="mt-1 text-xs text-foreground">{evidence.verification}</p>
                        </TD>
                        <TD>{entry.file_size_bytes == null ? "—" : formatBytes(entry.file_size_bytes)}</TD>
                        <TD className="min-w-40 max-w-[26rem] break-all font-mono text-xs">{entry.path}</TD>
                        <TD className="min-w-60 max-w-sm align-top">
                          <p className="text-xs font-medium">{entry.data_file_present ? "On this robot" : "Local file missing"}</p>
                          <p className="mt-1 text-xs text-foreground">{evidence.nextAction}</p>
                          <RecordingDetails recording={entry} path={entry.path} />
                        </TD>
                      </TR>
                      {open ? (
                        <TR>
                          <TD colSpan={6} className="bg-muted-foreground/5">
                            <dl className="grid gap-x-4 gap-y-1 p-2 text-xs sm:grid-cols-2">
                              <dt className="text-muted-foreground">SHA-256</dt>
                              <dd className="break-all font-mono">{entry.sha256_hash ?? "not recorded"}</dd>
                              <dt className="text-muted-foreground">Upload ID</dt>
                              <dd className="break-all font-mono">{entry.upload_id ?? "not recorded"}</dd>
                              <dt className="text-muted-foreground">Labels</dt>
                              <dd className="break-all font-mono">{entry.labels ? JSON.stringify(entry.labels) : "none"}</dd>
                            </dl>
                          </TD>
                        </TR>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </Table>
          </div>
          {history.data?.truncated ? (
            <div className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
              Showing the most recent {history.data.files.length} entries; more durable manifests exist on disk.
            </div>
          ) : null}
        </Card>
      )}
    </div>
  );
}
