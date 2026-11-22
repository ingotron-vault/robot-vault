import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { http } from "../../lib/api";
import { downloadAuthenticated } from "../../lib/download";
import { downloadCsv } from "../../lib/csv";
import { toLocalDatetimeInput } from "../../lib/datetime";
import { boundedContinuationToken } from "../../lib/pagination";
import { numberFilter, useUrlFilters } from "../../lib/urlFilters";
import { Button, Drawer, ProgressBar, SegmentedControl } from "@robot-vault/ui-kit";
import {
  Check,
  CircleAlert,
  Copy as CopyIcon,
  Download,
  FileArchive,
  Link2,
  Radio,
  RefreshCw,
  Search,
  UploadCloud,
  X,
} from "lucide-react";
import { useIsProEdition } from "../../components/Layout";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import { RecordingEvidence, processingLabel, recordingNextStep, recordingStatus, verificationLabel } from "./recordingStatus";
import "./Uploads.css";

interface UploadItem extends RecordingEvidence {
  upload_id: string;
  robot_id: string;
  original_filename?: string;
  storage_path: string;
  file_size_bytes: number;
  status: string;
  completed_at: number | null;
  bytes_received?: number;
}

interface UploadsResponse {
  items: UploadItem[];
  page: {
    limit: number;
    offset: number;
    returned: number;
    total_count: number;
    has_prev: boolean;
    has_next: boolean;
    next_offset: number | null;
    prev_offset: number | null;
    order_by: string;
    order_dir: string;
  };
}

interface UploadDetailTopic {
  topic: string;
  rows: number;
  parquet_bytes?: number | null;
  raw_bytes?: number | null;
  sources?: { name: string; raw_bytes: number }[];
}

interface UploadDetail extends RecordingEvidence {
  upload_id: string;
  robot_id: string;
  completed_at?: number | null;
  raw_bytes?: number;
  parquet_bytes?: number | null;
  raw_payload_sum?: number;
  rows_total?: number;
  ts_min?: string | null;
  ts_max?: string | null;
  original_filename?: string;
  storage_path?: string;
  raw_bytes_mode?: string;
  raw_bytes_error?: string;
  topics?: UploadDetailTopic[];
  // Capture-metadata intake: operator-declared session context, carried
  // through from the robot's vault.json. Null/absent for uploads that never
  // had them set.
  operator?: string | null;
  site?: string | null;
  permission_ref?: string | null;
  session_tags?: string | null;
  task_label?: string | null;
}

interface UploadFilesResp {
  parquet_keys?: Record<string, string[]>;
}

interface S3ListResp { items: { key: string; size: number; last_modified?: string | null }[]; is_truncated?: boolean; next_continuation_token?: string | null }

interface S3Upload { upload_id: string; robot_id: string; any_path: string }
type Discovery = { key: string; status: "idle" | "loading" | "completed" | "failed"; uploads: S3Upload[]; error?: string };

async function discoverS3Uploads(robot: string, since: string, until: string, signal: AbortSignal): Promise<S3Upload[]> {
  const robotsList = robot ? [robot] : await http<string[]>("/api/robots", { signal });
  const cutoff = since ? Date.parse(since) : Date.now() - 2 * 3600_000;
  const end = until ? Date.parse(until) : Infinity;
  const maxPerRobot = 2000;
  const found = new Map<string, S3Upload>();
  for (const robotId of robotsList) {
    let token: string | null = null;
    let scanned = 0;
    let pagesRead = 0;
    const seenTokens = new Set<string>();
    const maxPages = Math.ceil(maxPerRobot / 1000);
    do {
      if (signal.aborted) throw new DOMException("Scan cancelled", "AbortError");
      const params = new URLSearchParams({ prefix: `processed/parquet/${robotId}/`, max_keys: "1000" });
      if (token) params.set("continuation_token", token);
      const resp = await http<S3ListResp>(`/api/v1/s3/list?${params.toString()}`, { signal });
      for (const item of resp.items || []) {
        const modified = item.last_modified ? Date.parse(item.last_modified) : NaN;
        if (Number.isFinite(modified) && (modified < cutoff || modified > end)) continue;
        const match = (item.key || "").match(/part-([0-9a-fA-F-]{20,})\.parquet$/);
        if (match && !found.has(match[1])) {
          found.set(match[1], { upload_id: match[1], robot_id: robotId, any_path: item.key });
        }
      }
      scanned += (resp.items || []).length;
      pagesRead += 1;
      token = boundedContinuationToken(token, resp.next_continuation_token, seenTokens, pagesRead, maxPages);
      if (scanned >= maxPerRobot) break;
    } while (token);
  }
  return Array.from(found.values());
}

function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i += 1; }
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function epochToLocal(sec: number | null | undefined): string {
  if (!sec || sec <= 0) return "";
  const d = new Date(sec * 1000);
  return toLocalDatetimeInput(d.toISOString()).replace("T", " ") + `:${String(d.getSeconds()).padStart(2, "0")}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function uploadProgress(upload: UploadItem): number {
  if (upload.status === "completed") return 100;
  if (upload.status !== "uploading" || upload.file_size_bytes <= 0) return 0;
  return Math.max(0, Math.min(100, 100 * (upload.bytes_received || 0) / upload.file_size_bytes));
}

function robotIntakeState(robotId: string, visibleItems: UploadItem[]) {
  const robotUploads = visibleItems.filter((upload) => upload.robot_id === robotId);
  if (robotUploads.some((upload) => recordingStatus(upload) === "Needs attention")) return { state: "review", label: "Needs attention" };
  if (robotUploads.some((upload) => upload.status === "uploading")) return { state: "uploading", label: "Uploading" };
  if (robotUploads.length > 0) return { state: "active", label: "Active" };
  return { state: "idle", label: "No visible uploads" };
}

/** Tone for the derived recording-status label. The label always carries the meaning; tone only supports it. */
function recordingTone(label: string): "success" | "info" | "warning" | "danger" | "neutral" {
  switch (label) {
    case "Needs attention": return "danger";
    case "Ready": return "success";
    case "Processed · no rows": return "warning";
    case "Uploading":
    case "Processing":
    case "Waiting for processing": return "info";
    default: return "neutral";
  }
}

function RecordingChip({ upload }: { upload: RecordingEvidence }) {
  const label = recordingStatus(upload);
  return <span className={`rv-uploads__chip is-${recordingTone(label)}`}><i aria-hidden="true" />{label}</span>;
}

function transferLabel(upload: RecordingEvidence): string {
  if (upload.status === "completed") return "Received by the vault";
  if (upload.status === "uploading") return "Uploading to the vault";
  if (upload.status === "failed") return "Upload failed";
  return "Unknown";
}

type StepState = "done" | "current" | "review" | "failed" | "unknown";

/**
 * Received -> Verified -> Ready for queries, derived only from the transfer,
 * verification, and processing evidence that the details already show. Each
 * step is judged on its own evidence: processing does not wait for
 * verification, so a later step can be complete while an earlier one is unknown.
 */
function RecordingProgress({ upload }: { upload: RecordingEvidence }) {
  const transfer: StepState = upload.status === "completed" ? "done"
    : upload.status === "uploading" ? "current"
      : upload.status === "failed" ? "failed" : "unknown";
  const verifiedSha = upload.integrity_verified === true && upload.integrity_verification_basis === "client_sha256";
  const notVerified = upload.integrity_verified === false || upload.integrity_verification_basis === "unverified";
  const verification: StepState = verifiedSha ? "done" : notVerified ? "review" : "unknown";
  const ps = upload.processing_status || "";
  const processing: StepState = ps === "completed" ? "done"
    : ps === "completed_empty" ? "review"
      : ps === "failed" || ps === "dead_letter" ? "failed"
        : ps === "processing" || ps === "pending" ? "current" : "unknown";
  const steps: { title: string; detail: string; state: StepState }[] = [
    { title: "Received", detail: transferLabel(upload), state: transfer },
    { title: "Verified", detail: verificationLabel(upload), state: verification },
    {
      title: "Ready for queries",
      detail: ps === "completed"
        ? (upload.processing_completed_at ? `Processing finished ${evidenceTime(upload.processing_completed_at)}` : "Processing finished")
        : processingLabel(upload),
      state: processing,
    },
  ];
  return (
    <ol className="rv-uploads__steps" aria-label="Recording progress">
      {steps.map((step) => (
        <li key={step.title} data-state={step.state} aria-current={step.state === "current" ? "step" : undefined}>
          <span className="rv-uploads__step-bar" aria-hidden="true" />
          <span className="rv-uploads__step-title">
            {step.state === "done" ? <Check size={12} aria-hidden="true" /> : step.state === "failed" || step.state === "review" ? <CircleAlert size={12} aria-hidden="true" /> : null}
            {step.title}
          </span>
          <span className="rv-uploads__step-detail">{step.detail}</span>
        </li>
      ))}
    </ol>
  );
}

const QUICK_RANGES: { minutes: number; label: string }[] = [
  { minutes: 60, label: "1h" },
  { minutes: 1440, label: "24h" },
  { minutes: 10080, label: "7d" },
];

/** The preset whose length the current From/Until window matches, within a minute. */
function activeQuickRange(since: string, until: string): string {
  const from = Date.parse(since);
  const to = Date.parse(until);
  if (!Number.isFinite(from) || !Number.isFinite(to)) return "";
  const match = QUICK_RANGES.find((range) => Math.abs(to - from - range.minutes * 60_000) < 60_000);
  return match ? String(match.minutes) : "";
}

export function isoToDatetimeLocal(value: string): string {
  return toLocalDatetimeInput(value);
}

async function downloadObject(key: string): Promise<void> {
  await downloadAuthenticated(`/api/v1/s3/download?key=${encodeURIComponent(key)}`,
    key.split("/").pop() || "download");
}

export default function Uploads() {
  const queryClient = useQueryClient();
  const isPro = useIsProEdition();
  const [params, updateFilters] = useUrlFilters();
  const limit = numberFilter(params, "limit", 50);
  const offset = numberFilter(params, "offset", 0);
  const robot = params.get("robot_id") ?? "";
  const status = params.get("status") ?? "";
  const orderBy = params.get("order_by") ?? "completed_at";
  const orderDir = params.get("order_dir") ?? "desc";
  const q = params.get("q") ?? "";
  // Always include both completed and in-progress uploads
  const includeInProgress = true;
  const since = params.get("since") ?? "";
  const until = params.get("until") ?? "";
  const [density, setDensity] = useState<"comfortable" | "compact">("comfortable");
  // Auto-refresh is always enabled for live progress updates
  const auto = true;

  const robots = useQuery<string[]>({
    queryKey: ["robots"],
    queryFn: () => http<string[]>("/api/robots"),
    staleTime: 60_000,
  });

  useEffect(() => {
    if (!auto) return;
    // Use 10-second interval for upload progress updates as requested
    const t = setInterval(() => {
      ["uploads", "upload-detail", "upload-files"].forEach((key) => void queryClient.invalidateQueries({ queryKey: [key] }));
    }, 10_000);
    return () => clearInterval(t);
  }, [auto, queryClient]);

  const uploadsQ = useQuery<UploadsResponse>({
    queryKey: [
      "uploads",
      robot, status, orderBy, orderDir, q, includeInProgress,
      limit, offset, since, until,
    ],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      if (robot) params.set("robot_id", robot);
      if (status) params.set("status", status);
      if (orderBy) params.set("order_by", orderBy);
      if (orderDir) params.set("order_dir", orderDir);
      if (q) params.set("q", q);
      if (includeInProgress) params.set("include_in_progress", "true");
      if (since) params.set("since", since);
      if (until) params.set("until", until);
      return http<UploadsResponse>(`/api/v1/uploads/list?${params.toString()}`);
    },
    placeholderData: keepPreviousData,
  });

  const page = uploadsQ.data?.page;
  const items = uploadsQ.data?.items ?? [];
  const knownRobotIds = useMemo(
    () => Array.from(new Set([...(robots.data || []), ...items.map((item) => item.robot_id)])),
    [robots.data, items],
  );

  // Only inputs that affect S3 discovery form the key. Typing a search term or
  // changing table sort must not launch another fleet-wide scan.
  const discoveryKey = JSON.stringify([robot, since, until]);
  const [discovery, setDiscovery] = useState<Discovery>({ key: discoveryKey, status: "idle", uploads: [] });
  const attemptedKeys = useRef(new Set<string>());
  const activeScan = useRef<{ key: string; controller: AbortController } | null>(null);
  const currentDiscovery: Discovery = discovery.key === discoveryKey
    ? discovery : { key: discoveryKey, status: "idle", uploads: [] };
  const scanning = currentDiscovery.status === "loading";
  const s3Uploads = currentDiscovery.uploads;

  const scanS3Fallback = useCallback(() => {
    activeScan.current?.controller.abort();
    const controller = new AbortController();
    activeScan.current = { key: discoveryKey, controller };
    attemptedKeys.current.add(discoveryKey);
    setDiscovery({ key: discoveryKey, status: "loading", uploads: [] });
    void discoverS3Uploads(robot, since, until, controller.signal).then((uploads) => {
      if (activeScan.current?.controller !== controller || controller.signal.aborted) return;
      activeScan.current = null;
      setDiscovery({ key: discoveryKey, status: "completed", uploads });
    }).catch(() => {
      if (activeScan.current?.controller !== controller || controller.signal.aborted) return;
      activeScan.current = null;
      setDiscovery({ key: discoveryKey, status: "failed", uploads: [], error: "Could not scan S3 uploads. Try again." });
    });
  }, [discoveryKey, robot, since, until]);

  useEffect(() => () => {
    if (activeScan.current?.key === discoveryKey) {
      activeScan.current.controller.abort();
      activeScan.current = null;
      // An aborted request never settled. StrictMode effect replay and later
      // returns to this filter key may each make their one real attempt.
      attemptedKeys.current.delete(discoveryKey);
    }
  }, [discoveryKey]);

  // A settled empty or failed scan is still an attempt. Polling never retries it.
  useEffect(() => {
    if (since && !uploadsQ.isError && !uploadsQ.isFetching && items.length === 0 && !attemptedKeys.current.has(discoveryKey)) {
      scanS3Fallback();
    }
  }, [since, uploadsQ.isError, uploadsQ.isFetching, items.length, discoveryKey, scanS3Fallback]);

  // Detail drawer
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const [inspectedId, setInspectedId] = useState<string | null>(null);
  const selectedRobotId = robot || knownRobotIds[0] || "";
  const workspaceItems = selectedRobotId ? items.filter((item) => item.robot_id === selectedRobotId) : items;
  // A clicked table row is always the inspected capture, even when it belongs to
  // another robot than the summary panel; otherwise fall back to the robot's newest.
  const inspectedUpload = items.find((item) => item.upload_id === inspectedId) ?? workspaceItems[0] ?? null;
  const contextUploadId = drawerId ?? inspectedUpload?.upload_id ?? null;
  const workspaceBytes = workspaceItems.reduce((sum, item) => sum + item.file_size_bytes, 0);
  const workspaceCompleted = workspaceItems.filter((item) => item.status === "completed").length;
  const workspaceUploading = workspaceItems.filter((item) => item.status === "uploading").length;
  const workspaceFailed = workspaceItems.filter((item) => recordingStatus(item) === "Needs attention").length;
  const detail = useQuery<UploadDetail>({
    queryKey: ["upload-detail", contextUploadId],
    queryFn: () => http<UploadDetail>(`/api/v1/uploads/${encodeURIComponent(contextUploadId || "")}/detail`),
    enabled: !!contextUploadId,
  });
  const filesQ = useQuery<UploadFilesResp>({
    queryKey: ["upload-files", drawerId],
    queryFn: () => http<UploadFilesResp>(`/api/v1/uploads/${encodeURIComponent(drawerId || "")}/files`),
    enabled: !!drawerId,
  });
  const workspaceState = workspaceFailed > 0
    ? { state: "review", label: "Needs attention" }
    : workspaceUploading > 0
      ? { state: "uploading", label: "Uploading" }
      : workspaceItems.length > 0
        ? { state: "active", label: "Active" }
        : { state: "idle", label: "No uploads" };
  const workspaceTitle = selectedRobotId || "No robot selected";
  // The panel and drawer share one detail query. While the drawer is open the
  // query follows the drawer's upload, so only show its topics in the panel when
  // they belong to the inspected capture.
  const inspectedDetail = detail.data && detail.data.upload_id === inspectedUpload?.upload_id ? detail.data : undefined;
  const contextTopics = inspectedDetail?.topics ?? [];
  const activeFilterCount = [q, robot, status, since, until].filter(Boolean).length;
  const quickRange = activeQuickRange(since, until);

  return (
    <div className="rv-uploads">
      <PageHeader
        kicker="01 · Intake"
        title="Uploads"
        description="Monitor incoming robot captures, inspect processing progress, and open generated artifacts."
        actions={
          <>
            {auto ? <PageStatus tone="ok">Auto-refresh every 10s</PageStatus> : null}
            <Button
              variant="secondary"
              size="sm"
              onClick={() => ["uploads", "upload-detail", "upload-files"].forEach((key) => void queryClient.invalidateQueries({ queryKey: [key] }))}
            >
              <RefreshCw size={14} /> Refresh
            </Button>
            <Button variant="secondary" size="sm" onClick={() => exportCsv(items)}>
              <Download size={14} /> CSV
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                try {
                  navigator.clipboard?.writeText(window.location.href);
                } catch {
                  // Clipboard access may be unavailable in an offline browser.
                }
              }}
            >
              <Link2 size={14} /> Copy link
            </Button>
          </>
        }
      />

      <form className="rv-uploads__filters" role="search" aria-label="Filter captures" onSubmit={(event) => event.preventDefault()}>
        <label className="rv-uploads__field is-search">
          <span>Search</span>
          <div><Search size={15} aria-hidden="true" /><input type="search" placeholder="Filename, path, or upload ID" value={q} onChange={(e) => updateFilters({ q: e.target.value, offset: 0 })} /></div>
        </label>
        <label className="rv-uploads__field is-robot">
          <span>Robot</span>
          <select value={robot} onChange={(e) => { updateFilters({ robot_id: e.target.value, offset: 0 }); setInspectedId(null); }}>
            <option value="">All robots</option>
            {(robots.data || []).map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <div className="rv-uploads__field is-segmented">
          <span aria-hidden="true">Transfer status</span>
          <SegmentedControl
            className="rv-uploads__seg"
            label="Transfer status"
            value={status}
            onChange={(value) => updateFilters({ status: value, offset: 0 })}
            options={[
              { value: "", label: "All" },
              { value: "completed", label: "Received" },
              { value: "uploading", label: "Uploading" },
              { value: "failed", label: "Failed" },
            ]}
          />
        </div>
        <label className="rv-uploads__field is-date">
          <span>From</span>
          <input
            type="datetime-local"
            value={isoToDatetimeLocal(since)}
            onChange={(e) => updateFilters({ since: e.target.value ? new Date(e.target.value).toISOString() : null, offset: 0 })}
          />
        </label>
        <label className="rv-uploads__field is-date">
          <span>Until</span>
          <input
            type="datetime-local"
            value={isoToDatetimeLocal(until)}
            onChange={(e) => updateFilters({ until: e.target.value ? new Date(e.target.value).toISOString() : null, offset: 0 })}
          />
        </label>
        <div className="rv-uploads__field is-segmented">
          <span aria-hidden="true">Quick range</span>
          <SegmentedControl
            className="rv-uploads__seg"
            label="Quick range"
            value={quickRange}
            onChange={(value) => {
              const to = new Date();
              const from = new Date(to.getTime() - Number(value) * 60 * 1000);
              updateFilters({ since: from.toISOString(), until: to.toISOString(), offset: 0 });
            }}
            options={QUICK_RANGES.map((range) => ({ value: String(range.minutes), label: range.label }))}
          />
        </div>
        <label className="rv-uploads__field is-sort">
          <span>Sort by</span>
          <select value={orderBy} onChange={(e) => updateFilters({ order_by: e.target.value, offset: 0 })}>
            <option value="completed_at">Received at</option>
            <option value="file_size_bytes">File size</option>
            <option value="original_filename">Filename</option>
            <option value="robot_id">Robot</option>
          </select>
        </label>
        <label className="rv-uploads__field is-direction">
          <span>Direction</span>
          <select value={orderDir} onChange={(e) => updateFilters({ order_dir: e.target.value, offset: 0 })}>
            <option value="desc">Descending</option>
            <option value="asc">Ascending</option>
          </select>
        </label>
        <label className="rv-uploads__field is-limit">
          <span>Rows</span>
          <input type="number" min={1} max={500} value={limit} onChange={(e) => updateFilters({ limit: Number(e.target.value || 50), offset: 0 })} />
        </label>
        <div className="rv-uploads__field is-segmented">
          <span aria-hidden="true">Density</span>
          <SegmentedControl
            className="rv-uploads__seg"
            label="Upload table density"
            value={density}
            onChange={setDensity}
            options={[
              { value: "comfortable", label: "Comfortable" },
              { value: "compact", label: "Compact" },
            ]}
          />
        </div>
        {activeFilterCount ? (
          <button
            type="button"
            className="rv-uploads__clear"
            onClick={() => { updateFilters({ q: null, robot_id: null, status: null, since: null, until: null, offset: 0 }); setInspectedId(null); }}
          >
            <X size={14} aria-hidden="true" /> Clear filters ({activeFilterCount})
          </button>
        ) : null}
      </form>

      <section className="rv-uploads__fleet" aria-labelledby="rv-intake-robots-title">
        <div className="rv-uploads__fleet-robots">
          <div className="rv-uploads__panel-heading">
            <h2 id="rv-intake-robots-title">Robots</h2>
            <span className="rv-uploads__count">{knownRobotIds.length}</span>
          </div>
          <div className="rv-uploads__robot-list">
            {knownRobotIds.map((robotId) => {
              const robotState = robotIntakeState(robotId, items);
              const visibleCount = items.filter((item) => item.robot_id === robotId).length;
              return (
                <button
                  type="button"
                  key={robotId}
                  className={selectedRobotId === robotId ? "is-selected" : ""}
                  onClick={() => { updateFilters({ robot_id: robotId, offset: 0 }); setInspectedId(null); }}
                  aria-pressed={selectedRobotId === robotId}
                >
                  <i className={`rv-uploads__dot is-${robotState.state}`} aria-hidden="true" />
                  <span><b>{robotId}</b><small>{visibleCount} visible capture{visibleCount === 1 ? "" : "s"}</small></span>
                  <em>{robotState.label}</em>
                </button>
              );
            })}
            {robots.isLoading ? <p role="status">Loading robots…</p> : null}
            {robots.isError ? <p role="alert">Robot list unavailable.</p> : null}
          </div>
        </div>

        <section className="rv-uploads__dossier" aria-labelledby="rv-intake-dossier-title">
          <div className="rv-uploads__dossier-identity">
            <small>{selectedRobotId ? "Robot intake" : "Fleet intake"}</small>
            <h2 id="rv-intake-dossier-title">{workspaceTitle}</h2>
            <p>{selectedRobotId ? `${workspaceItems.length} captures on this page` : `${knownRobotIds.length} registered robots`}</p>
            <span className={`rv-uploads__workspace-status is-${workspaceState.state}`}><i aria-hidden="true" />{workspaceState.label}</span>
          </div>
          <dl className="rv-uploads__dossier-facts">
            <div><dt>Visible captures</dt><dd>{workspaceItems.length}</dd></div>
            <div><dt>Received</dt><dd>{workspaceCompleted}</dd></div>
            <div><dt>In flight</dt><dd className={workspaceUploading ? "is-accent" : ""}>{workspaceUploading}</dd></div>
            <div><dt>Needs attention</dt><dd className={workspaceFailed ? "is-danger" : ""}>{workspaceFailed}</dd></div>
            <div><dt>Payload</dt><dd>{formatBytes(workspaceBytes)}</dd></div>
          </dl>
        </section>
      </section>

      <div className="rv-uploads__workspace">
        <section className="rv-uploads__register" aria-labelledby="rv-upload-register-title">
          <div className="rv-uploads__register-heading">
            <div>
              <h2 id="rv-upload-register-title"><FileArchive size={16} aria-hidden="true" /> Captures</h2>
              <p className="rv-uploads__result-meta">
                <strong>{page?.total_count ?? 0}</strong> uploads
                <span>Robot: {robot || "all"}</span>
                <span>Sort: {orderBy} {orderDir}</span>
                {(since || until) ? <span>Filtered by capture time</span> : null}
              </p>
            </div>
            <p>Select a row to inspect metadata, topics, and generated files.</p>
          </div>
          <div className="rv-uploads__table-wrap">
            <table className={`rv-uploads__table ${density === "compact" ? "is-compact" : "is-comfortable"}`}>
              <thead>
                <tr>
                  {headCell("upload_id", "Upload ID")}
                  {headCell("robot_id", "Robot")}
                  {headCell("original_filename", "Filename")}
                  {headCell("completed_at", "Received at")}
                  <th scope="col">Recording status</th>
                  {headCell("file_size_bytes", "Size", "is-num")}
                  <th scope="col">Transfer progress</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {uploadsQ.isLoading && (
                  <tr><td className="rv-uploads__table-state" colSpan={8}>Loading…</td></tr>
                )}
                {uploadsQ.isError && (
                  <tr><td className="rv-uploads__table-state is-error" colSpan={8}>Could not load uploads. Use Refresh to try again.</td></tr>
                )}
                {!uploadsQ.isLoading && !uploadsQ.isError && items.length === 0 && (
                  <tr><td className="rv-uploads__table-state" colSpan={8}>
                    <div className="rv-uploads__empty">
                      <p>No indexed uploads found for the selected filters.</p>
                      <button type="button" className="rv-uploads__button" onClick={scanS3Fallback} disabled={scanning}>{scanning ? "Scanning S3…" : currentDiscovery.status === "failed" ? "Retry S3 scan" : `Scan S3 for uploads${since ? " (since filter)" : ""}`}</button>
                      {currentDiscovery.error && <p role="alert" className="rv-uploads__error-text">{currentDiscovery.error}</p>}
                      {s3Uploads.length > 0 && (
                        <p className="rv-uploads__muted">Found {s3Uploads.length} uploads via S3; showing below.</p>
                      )}
                    </div>
                  </td></tr>
                )}
                {items.map((u) => {
                  let progressText = "";
                  let progressDetail: string | null = null;
                  let progressBar = null;
                  let progressState = "unknown";

                  if (u.status === "uploading" && u.bytes_received != null && (u.file_size_bytes || 0) > 0) {
                    const bytesReceived = u.bytes_received || 0;
                    const pctValue = Math.max(
                      0,
                      Math.min(100, 100.0 * bytesReceived / u.file_size_bytes),
                    );
                    const mbReceived = (bytesReceived / (1024 * 1024)).toFixed(1);
                    const mbTotal = (u.file_size_bytes / (1024 * 1024)).toFixed(1);
                    progressText = `${pctValue.toFixed(1)}%`;
                    progressDetail = `${mbReceived}/${mbTotal} MB`;
                    progressState = "uploading";
                    progressBar = (
                      <ProgressBar
                        className="rv-uploads__bar"
                        label={`Upload ${u.upload_id} progress`}
                        value={pctValue}
                      />
                    );
                  } else if (u.status === "completed") {
                    progressText = "100%";
                    progressState = "completed";
                    progressBar = <span className="rv-uploads__bar is-static" aria-hidden="true"><span /></span>;
                  } else if (u.status === "failed") {
                    progressText = "Failed";
                    progressState = "failed";
                  } else {
                    progressText = "Unknown";
                  }
                  const selected = inspectedUpload?.upload_id === u.upload_id;

                  return (
                    <tr key={u.upload_id} className={`rv-uploads__row${selected ? " is-selected" : ""}`} onClick={() => { setInspectedId(u.upload_id); setDrawerId(u.upload_id); }}>
                      <td><span className="rv-uploads__id-cell"><code className="rv-uploads__id" title={u.upload_id}>{u.upload_id}</code><Copy text={u.upload_id} label="Copy" /></span></td>
                      <td><span className="rv-uploads__robot">{u.robot_id}</span></td>
                      <td>
                        <div className="rv-uploads__file">
                          <span><span className="rv-uploads__filename">{u.original_filename || "Unnamed capture"}</span><Copy text={u.original_filename || ""} label="Copy" /></span>
                          <small title={u.storage_path}>{u.storage_path}</small>
                        </div>
                      </td>
                      <td className="is-mono" title={epochToLocal(u.completed_at)}>{epochToLocal(u.completed_at) || "Not recorded"}</td>
                      <td><RecordingChip upload={u} /></td>
                      <td className="is-num is-mono">{formatBytes(u.file_size_bytes)}</td>
                      <td>
                        <div className={`rv-uploads__progress is-${progressState}`}>
                          {progressBar}
                          <span className="rv-uploads__progress-text">{progressText}</span>
                          {progressDetail ? <small>{progressDetail}</small> : null}
                        </div>
                      </td>
                      <td>
                        <div className="rv-uploads__row-actions">
                          <button type="button" onClick={(e) => { e.stopPropagation(); setDrawerId(u.upload_id); }}>Details</button>
                          <Link onClick={(e) => e.stopPropagation()} to={`/explore?robot=${encodeURIComponent(u.robot_id)}`}>Open chart</Link>
                          {isPro ? (
                            <Link
                              onClick={(event) => event.stopPropagation()}
                              to={`/episodes?robot_id=${encodeURIComponent(u.robot_id)}`}
                            >
                              Episodes
                            </Link>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {items.length === 0 && s3Uploads.length > 0 && s3Uploads.slice(0, 200).map((u) => (
                  <tr key={`s3-${u.upload_id}`} className="rv-uploads__row">
                    <td><code className="rv-uploads__id" title={u.upload_id}>{u.upload_id}</code></td>
                    <td>{u.robot_id}</td>
                    <td>—</td>
                    <td>—</td>
                    <td><span className="rv-uploads__chip is-neutral">(from S3)</span></td>
                    <td className="is-num">—</td>
                    <td>—</td>
                    <td><div className="rv-uploads__row-actions"><Link to={`/explore?robot=${encodeURIComponent(u.robot_id)}`}>Open chart</Link></div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="rv-uploads__pagination" aria-label="Upload pages">
            <span>{page?.returned ?? 0} shown · offset {page?.offset ?? offset}</span>
            <div>
              <button
                type="button"
                disabled={!page?.has_prev}
                onClick={() => page?.prev_offset != null && updateFilters({ offset: page.prev_offset })}
              >Previous</button>
              <button
                type="button"
                disabled={!page?.has_next}
                onClick={() => page?.next_offset != null && updateFilters({ offset: page.next_offset })}
              >Next</button>
            </div>
          </nav>
        </section>

        <aside className="rv-uploads__inspector" aria-labelledby="rv-inspector-kicker rv-inspector-title">
          <div className="rv-uploads__inspector-head">
            <span id="rv-inspector-kicker" className="rv-uploads__kicker">Upload details</span>
            <h2 id="rv-inspector-title">{inspectedUpload ? (inspectedUpload.original_filename || inspectedUpload.upload_id) : "No capture selected"}</h2>
          </div>

          {inspectedUpload ? (
            <>
              <RecordingProgress upload={inspectedUpload} />
              <div className="rv-uploads__inspector-body">
                {(inspectedUpload.status === "completed" || (inspectedUpload.status === "uploading" && inspectedUpload.bytes_received != null && inspectedUpload.file_size_bytes > 0)) ? <ProgressBar
                  className="rv-uploads__bar is-wide"
                  label={`Selected upload ${inspectedUpload.upload_id} progress`}
                  value={uploadProgress(inspectedUpload)}
                /> : null}
                <dl className="rv-uploads__kv">
                  <div><dt>State</dt><dd><RecordingChip upload={inspectedUpload} /></dd></div>
                  <div><dt>Upload ID</dt><dd className="is-mono">{inspectedUpload.upload_id}</dd></div>
                  <div><dt>Robot</dt><dd>{inspectedUpload.robot_id}</dd></div>
                  <div><dt>Payload</dt><dd className="is-mono">{formatBytes(inspectedUpload.file_size_bytes)}</dd></div>
                  <div><dt>Received</dt><dd className="is-mono">{epochToLocal(inspectedUpload.completed_at) || "Not recorded"}</dd></div>
                  <div><dt>Processed rows</dt><dd className="is-mono">{inspectedDetail?.rows_total?.toLocaleString() ?? "—"}</dd></div>
                  <div><dt>Next step</dt><dd>{recordingNextStep(inspectedUpload)}</dd></div>
                </dl>
                <div className="rv-uploads__inspector-actions">
                  <button type="button" className="rv-uploads__button is-primary" onClick={() => setDrawerId(inspectedUpload.upload_id)}>Open details</button>
                  <Link className="rv-uploads__button" to={`/explore?robot=${encodeURIComponent(inspectedUpload.robot_id)}`}>Open chart</Link>
                </div>
              </div>

              <section className="rv-uploads__sensor-context" aria-labelledby="rv-sensor-context-title">
                <h3 id="rv-sensor-context-title" className="rv-uploads__kicker">{contextTopics.length || "No"} processed topics</h3>
                {detail.isLoading ? <p role="status">Loading topic context…</p> : null}
                {detail.isError ? <p>Topic context is unavailable for this capture.</p> : null}
                {!detail.isLoading && !detail.isError && contextTopics.length === 0 ? (
                  <p>No processed topic data is available for the selected capture.</p>
                ) : null}
                {contextTopics.length > 0 ? (
                  <ul>
                    {contextTopics.slice(0, 5).map((topic) => (
                      <li key={topic.topic}>
                        <span title={topic.topic}>{topic.topic}</span>
                        <span>{topic.rows.toLocaleString()} rows</span>
                        <small>{formatBytes(topic.parquet_bytes)}</small>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>

              {inspectedDetail && (inspectedDetail.site || inspectedDetail.operator || inspectedDetail.task_label) ? (
                <div className="rv-uploads__capture-context">
                  <Radio size={14} aria-hidden="true" />
                  <span>{inspectedDetail.task_label || "Unlabelled capture"}</span>
                  {inspectedDetail.site ? <small>{inspectedDetail.site}</small> : null}
                  {inspectedDetail.operator ? <small>Operator {inspectedDetail.operator}</small> : null}
                </div>
              ) : null}
            </>
          ) : (
            <p className="rv-uploads__inspector-empty">Select a capture in the table to see its transfer, verification, and processing evidence.</p>
          )}

          <section className="rv-uploads__recent" aria-labelledby="rv-recent-uploads-title">
            <div className="rv-uploads__panel-heading">
              <h3 id="rv-recent-uploads-title" className="rv-uploads__kicker">Recent captures · {selectedRobotId || "fleet"}</h3>
              <span className="rv-uploads__count">{workspaceItems.length}</span>
            </div>
            <div className="rv-uploads__recent-list">
              {workspaceItems.slice(0, 5).map((upload) => (
                <button
                  type="button"
                  key={upload.upload_id}
                  className={inspectedUpload?.upload_id === upload.upload_id ? "is-selected" : ""}
                  onClick={() => setInspectedId(upload.upload_id)}
                  aria-pressed={inspectedUpload?.upload_id === upload.upload_id}
                >
                  <span className={`rv-uploads__recent-mark is-${recordingStatus(upload) === "Needs attention" ? "failed" : upload.status}`} aria-hidden="true">
                    {recordingStatus(upload) === "Needs attention" ? <CircleAlert size={13} /> : upload.status === "completed" ? <Check size={13} /> : <UploadCloud size={13} />}
                  </span>
                  <span><b>{upload.original_filename || upload.upload_id}</b><small>{upload.robot_id}</small></span>
                  <em>{upload.status === "uploading" && upload.bytes_received != null && upload.file_size_bytes > 0 ? `${Math.round(uploadProgress(upload))}% transferred` : recordingStatus(upload)}</em>
                </button>
              ))}
              {!uploadsQ.isLoading && workspaceItems.length === 0 ? <p>No captures match this workspace.</p> : null}
            </div>
          </section>
        </aside>
      </div>

      <Drawer
        open={!!drawerId}
        onClose={() => setDrawerId(null)}
        title="Upload details"
        size="xl"
      >
        <div className="rv-uploads__drawer">
          <div className="rv-uploads__drawer-toolbar">
            <Button variant="secondary" size="sm" onClick={() => { void detail.refetch(); void filesQ.refetch(); }}>
              <RefreshCw size={14} /> Refresh recording
            </Button>
          </div>
          {detail.isLoading && <p role="status">Loading…</p>}
          {detail.isError && <p className="rv-uploads__error-text">Could not load recording details. Use Refresh to try again.</p>}
          {detail.data && (
            <>
              <RecordingProgress upload={detail.data} />
              <RecordingStatusDetails upload={detail.data} />
              <dl className="rv-uploads__kv is-grid">
                <div><dt>Upload ID</dt><dd><code>{drawerId || ""}</code> <Copy text={drawerId || ""} label="Copy" /></dd></div>
                <div><dt>Robot</dt><dd>{detail.data.robot_id}</dd></div>
                <div><dt>Received at</dt><dd className="is-mono">{epochToLocal(detail.data.completed_at ?? null) || "Not recorded"}</dd></div>
                <div><dt>Raw bytes</dt><dd className="is-mono">{formatBytes(detail.data.raw_bytes)}</dd></div>
                <div><dt>Raw payload sum</dt><dd className="is-mono">{formatBytes(detail.data.raw_payload_sum)}</dd></div>
                <div><dt>Parquet bytes</dt><dd className="is-mono">{formatBytes(detail.data.parquet_bytes)}</dd></div>
                <div><dt>Rows total</dt><dd className="is-mono">{detail.data.rows_total ?? "Unknown"}</dd></div>
                <div><dt>Time range</dt><dd className="is-mono">{detail.data.ts_min && detail.data.ts_max ? `${detail.data.ts_min} → ${detail.data.ts_max}` : "—"}</dd></div>
                <div><dt>Filename</dt><dd>{detail.data.original_filename || ""}</dd></div>
                <div><dt>Registered storage location</dt><dd className="is-mono is-break">{detail.data.storage_path || "Unknown"} <Copy text={detail.data.storage_path || ""} label="Copy" /></dd></div>
              </dl>

              {(detail.data.operator || detail.data.site || detail.data.permission_ref || detail.data.task_label || detail.data.session_tags) && (
                <section className="rv-uploads__drawer-section" aria-labelledby="rv-drawer-context-title">
                  <h3 id="rv-drawer-context-title">Capture context</h3>
                  <dl className="rv-uploads__kv is-grid">
                    {detail.data.operator && <div><dt>Operator</dt><dd>{detail.data.operator}</dd></div>}
                    {detail.data.site && <div><dt>Site</dt><dd>{detail.data.site}</dd></div>}
                    {detail.data.permission_ref && <div><dt>Permission ref</dt><dd>{detail.data.permission_ref}</dd></div>}
                    {detail.data.task_label && <div><dt>Task</dt><dd>{detail.data.task_label}</dd></div>}
                    {detail.data.session_tags && (
                      <div>
                        <dt>Session tags</dt>
                        <dd className="rv-uploads__tags">
                          {detail.data.session_tags.split(",").map((t) => t.trim()).filter(Boolean).map((tag) => (
                            <span key={tag} className="rv-uploads__chip is-neutral">{tag}</span>
                          ))}
                        </dd>
                      </div>
                    )}
                  </dl>
                </section>
              )}

              <section className="rv-uploads__drawer-section" aria-labelledby="rv-drawer-downloads-title">
                <h3 id="rv-drawer-downloads-title">Downloads</h3>
                {filesQ.isLoading ? <p role="status">Loading generated files…</p> : null}
                {filesQ.isError ? <p role="alert">Could not load generated files. Use Refresh to try again.</p> : null}
                {filesQ.isSuccess && !Object.values(filesQ.data.parquet_keys || {}).some((keys) => keys.length) ? <p>No generated files are listed for this recording.</p> : null}
                <div className="rv-uploads__downloads">
                  {Object.entries(filesQ.data?.parquet_keys || {}).map(([t, keys]) => (
                    keys.map((k) => <ObjectDownloadButton key={k} keyName={k} label={`Parquet: ${t}`} />)
                  ))}
                </div>
              </section>

              <section className="rv-uploads__drawer-section" aria-labelledby="rv-drawer-topics-title">
                <h3 id="rv-drawer-topics-title">Topics</h3>
                <div className="rv-uploads__table-wrap">
                  <table className="rv-uploads__topics">
                    <thead>
                      <tr><th scope="col"><span className="sr-only">Expand</span></th><th scope="col">Topic</th><th scope="col" className="is-num">Rows</th><th scope="col" className="is-num">Parquet</th><th scope="col" className="is-num">Raw</th><th scope="col">Actions</th></tr>
                    </thead>
                    <tbody>
                      {(detail.data.topics || []).length === 0 && (
                        <tr><td colSpan={6} className="rv-uploads__table-state">No Parquet data available for this upload.</td></tr>
                      )}
                      {(detail.data.topics || []).map((t) => (
                        <TopicRow key={t.topic} topic={t} robot={detail.data.robot_id} since={detail.data.ts_min || ""} until={detail.data.ts_max || ""} storagePath={detail.data.storage_path || ""} parquetKeysByTopic={filesQ.data?.parquet_keys || {}} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
              {detail.data.raw_bytes_mode === "error" && detail.data.raw_bytes_error ? (
                <p className="rv-uploads__error-text">Raw parse error: {detail.data.raw_bytes_error}</p>
              ) : null}
            </>
          )}
        </div>
      </Drawer>
    </div>
  );

  function headCell(key: string, label: string, extra?: string) {
    const isActive = orderBy === key;
    const nextDir = isActive && orderDir === "desc" ? "asc" : "desc";
    return (
      <th
        scope="col"
        className={extra || undefined}
        aria-sort={isActive ? (orderDir === "desc" ? "descending" : "ascending") : "none"}
      >
        <button
          type="button"
          className="rv-uploads__sort"
          onClick={() => updateFilters({ order_by: key, order_dir: nextDir, offset: 0 })}
          title="Sort column"
        >
          {label}{" "}{isActive ? (orderDir === "desc" ? "▼" : "▲") : ""}
        </button>
      </th>
    );
  }

  function exportCsv(rows: UploadItem[]) {
    try {
      downloadCsv(
        "uploads.csv",
        ["upload_id", "robot", "filename", "completed", "status", "file_size_bytes"],
        rows.map((u) => [
          u.upload_id,
          u.robot_id,
          u.original_filename || "",
          epochToLocal(u.completed_at),
          u.status,
          u.file_size_bytes,
        ]),
      );
    } catch {}
  }

}

function evidenceTime(value?: string | null): string {
  if (!value) return "Unknown";
  const time = new Date(value);
  return Number.isFinite(time.getTime()) ? time.toLocaleString() : "Unknown";
}

export function RecordingStatusDetails({ upload }: { upload: UploadDetail }) {
  return (
    <section className="rv-uploads__recording-details" aria-label="Recording status">
      <h3>{recordingStatus(upload)}</h3>
      <dl>
        <div><dt>Transfer</dt><dd>{transferLabel(upload)}</dd></div>
        <div><dt>Server verification</dt><dd>{verificationLabel(upload)}</dd></div>
        <div><dt>Processing</dt><dd>{processingLabel(upload)}</dd></div>
        <div><dt>Last upload update</dt><dd>{evidenceTime(upload.upload_updated_at)}</dd></div>
        <div><dt>Last processing update</dt><dd>{evidenceTime(upload.processing_updated_at)}</dd></div>
        <div><dt>Processing attempts</dt><dd>{upload.processing_attempts ?? "Unknown"}</dd></div>
        {upload.processing_started_at ? <div><dt>Processing started</dt><dd>{evidenceTime(upload.processing_started_at)}</dd></div> : null}
        {upload.processing_completed_at ? <div><dt>Processing finished</dt><dd>{evidenceTime(upload.processing_completed_at)}</dd></div> : null}
      </dl>
      {upload.upload_error ? <p className="rv-uploads__recording-error"><strong>Upload error</strong> {upload.upload_error}</p> : null}
      {upload.processing_error ? <p className="rv-uploads__recording-error"><strong>Processing error</strong> {upload.processing_error}</p> : null}
      <p><strong>Next step</strong> {recordingNextStep(upload)}</p>
      <p><strong>Robot copy</strong> Unknown here. Check the robot's local copy, current server storage, and retention policy before deleting a recording.</p>
      <p className="rv-uploads__recording-note">The storage location below is the vault's registered path. This view does not check whether the object still exists.</p>
    </section>
  );
}

function Copy({ text, label }: { text: string; label: string }) {
  return (
    <button type="button" className="rv-uploads__copy" title={label} onClick={(e)=>{ e.stopPropagation(); if (text) navigator.clipboard?.writeText(text).catch(()=>{}); }}><CopyIcon size={13} aria-hidden="true" /><span className="sr-only">{label}</span></button>
  );
}

function TopicRow({ topic, robot, since, until, storagePath, parquetKeysByTopic }: { topic: UploadDetailTopic; robot: string; since: string; until: string; storagePath: string; parquetKeysByTopic: Record<string, string[]>; }) {
  const [expanded, setExpanded] = useState(false);
  const toggle = (e?: React.MouseEvent) => { if (e) e.stopPropagation(); setExpanded((s)=> !s); };
  const keys = parquetKeysByTopic?.[topic.topic] || [];
  return (
    <>
      <tr className={`rv-uploads__topic-row${expanded ? " is-expanded" : ""}`} onClick={toggle}>
        <td><button type="button" className="rv-uploads__disclosure" aria-expanded={expanded} aria-label={`${expanded ? "Hide" : "Show"} files for ${topic.topic}`} onClick={toggle}><span aria-hidden="true">{expanded ? "▾" : "▸"}</span></button></td>
        <td className="is-mono">{topic.topic}</td>
        <td className="is-num is-mono">{topic.rows}</td>
        <td className="is-num is-mono">{formatBytes(topic.parquet_bytes)}</td>
        <td className="is-num is-mono">{topic.raw_bytes == null ? "—" : formatBytes(topic.raw_bytes)}</td>
        <td><button type="button" className="rv-uploads__button is-small" disabled={!since || !until} onClick={(e)=>{ e.stopPropagation(); const p = new URLSearchParams(); p.set("robot", robot); p.set("topic", topic.topic); if (since) p.set("since", since); if (until) p.set("until", until); window.location.href = `/ui/explore/?${p.toString()}`; }}>Open in chart</button></td>
      </tr>
      {expanded && (
        <tr className="rv-uploads__subrow"><td colSpan={6}>
          <p className="rv-uploads__muted">Raw file: <span className="is-mono is-break">{storagePath || "—"}</span></p>
          <p className="rv-uploads__subrow-label">Parquet files ({keys.length}):</p>
          <div className="rv-uploads__downloads">
            {keys.length === 0 && <p className="rv-uploads__muted">No parquet files listed for this topic.</p>}
            {keys.map((k) => (<ObjectDownloadButton key={k} keyName={k} label={k.split("/").slice(-2).join("/")} />))}
          </div>
          <p className="rv-uploads__subrow-label">Raw sources for this topic:</p>
          <div className="rv-uploads__sources">
            {(topic.sources || []).length === 0 && <p className="rv-uploads__muted">No raw subtopics detected in MCAP for this topic.</p>}
            {(topic.sources || []).map((s) => (
              <div key={s.name}>
                <code>{s.name}</code>
                <span className="is-mono">{formatBytes(s.raw_bytes)}</span>
              </div>
            ))}
          </div>
        </td></tr>
      )}
    </>
  );
}

export function ObjectDownloadButton({ keyName, label }: { keyName: string; label: string }) {
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const download = async () => {
    if (state === "loading") return;
    setState("loading");
    setError(null);
    try {
      await downloadObject(keyName);
      setState("idle");
    } catch (failure) {
      setState("error");
      setError(failure instanceof Error ? failure.message : "Download failed. Try again.");
    }
  };
  return (
    <span className="rv-uploads__download">
    <button
      className="rv-uploads__button is-small"
      type="button"
      disabled={state === "loading"}
      aria-busy={state === "loading"}
      title={error ?? undefined}
      onClick={() => void download()}
    >
      {state === "loading" ? "Preparing…" : state === "error" ? `Retry ${label}` : label}
    </button>
    {error ? <span className="rv-uploads__error-text" role="alert">{error}</span> : null}
    </span>
  );
}
