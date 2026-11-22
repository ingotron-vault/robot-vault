import axios from "axios";

// Relative baseURL: in dev, vite.config.ts proxies /api + /metrics to the
// agent's FastAPI server; in prod the SPA is served by that same FastAPI
// process (see web.py's StaticFiles mount), so same-origin relative paths
// work in both cases without any build-time configuration.
export const http = axios.create({ baseURL: "" });

// Mirrors web.py's BASE_ROOT, the outer filesystem policy boundary and the
// root used before a specific robot is selected.
export const BASE_ROOT = "/tmp/robot_data";

// ---------------------------------------------------------------------------
// Shared shapes
//
// Every interface below is a literal mirror of what web.py actually returns
// (see phases/02-robot-upload-agent/src/robot_upload_agent/web.py and the
// phase 02 technical guide). Six former client endpoints had no backend route
// (/api/overview, /api/retention, /api/stats, /api/settings, POST
// /api/upload/jobs/{id}/cancel, DELETE /api/upload/jobs) -- those are gone,
// along with the types that only existed to describe their responses.
// ---------------------------------------------------------------------------

export interface Robot {
  name: string;
  path: string;
  mcap_count: number;
  uploaded_count: number;
  pending_count: number;
  failed_count: number;
  total_size_bytes: number;
  last_modified: number;
}

export interface DiscoverRobotsResponse {
  robots: Robot[];
  error?: string;
}

// UploadQueue.describe() exposes the active concurrency and retry policy.
export interface BatchStrategy {
  parallel_streams: number;
  retry_multiplier: number;
}

export interface StatusResponse {
  root: string;
  pending_count: number;
  connected: boolean;
  server_reachable: boolean;
  server_host: string;
  server_port: number;
  network_type: string;
  network_interface: string | null;
  network_ssid: string | null;
  is_wifi: boolean;
  is_cellular: boolean;
  is_ethernet: boolean;
  connection_quality: string;
  batch_strategy: BatchStrategy;
  pending: string[];
}

export interface RecordingEvidence {
  data_file_present?: boolean;
  // A receipt proves verification at upload time, not current server presence.
  // Never infer verification or removal permission from completed alone.
  server_verified?: boolean | null;
  server_verified_at?: string | null;
  server_storage_path?: string | null;
  safe_to_remove_local?: boolean;
  last_upload_error?: Record<string, unknown> | null;
  upload_updated_at?: string | null;
  saved_bytes?: number;
  resume_supported?: boolean | null;
}

export interface FileItem extends RecordingEvidence {
  path: string;
  name: string;
  size: number;
  mtime: number;
  status: "pending" | "uploading" | "completed" | "failed" | string;
}

// web.py's JobStore only ever sets a file to pending/uploading/completed/
// failed (robot_vault_config.upload_status.UploadStatus) -- there is no
// "cancel" endpoint, so "cancelled" is never produced.
export interface JobFile {
  path: string;
  bytes_sent: number;
  total_bytes: number;
  status: "pending" | "uploading" | "completed" | "failed" | string;
  error: Record<string, unknown> | null;
}

// GET /api/upload/jobs and GET /api/upload/jobs/{job_id} never return a
// `root` -- JobStore.create_job is only ever given a flat file list. Do not
// re-add it here; derive a root from `files[].path` at the call site if one
// is genuinely needed (see RobotContext's reattach-on-mount for an example).
export interface Job {
  job_id: string;
  status: "running" | "completed" | string;
  started_at: number;
  completed_at: number | null;
  files: JobFile[];
}

export interface HistoryEntry extends RecordingEvidence {
  path: string;
  status: "pending" | "uploading" | "completed" | "failed" | string;
  file_size_bytes: number | null;
  sha256_hash: string | null;
  upload_id: string | null;
  recorded_at: string | null;
  created_at: string | null;
  labels: Record<string, unknown> | null;
  // The manifest may outlive the data file. Absence does not prove a
  // successful upload or that any server copy is still available.
  data_file_present: boolean;
}

export interface HistoryResponse {
  root: string;
  files: HistoryEntry[];
  truncated: boolean;
}

export interface UploadOptions {
  bandwidth_mbps?: number;
  chunk_size_bytes?: number;
  compute_chunk_hashes?: boolean;
}

// POST /api/upload/resume-all and POST /api/upload/retry-failed both return
// UploadQueue.run_many()/upload_selected()'s result: `uploaded` + `errors`
// only, never `cancelled` (there is no cancel path).
export interface ActionResult {
  uploaded: string[];
  errors: { file: string; error: Record<string, unknown> }[];
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

export async function discoverRobots(): Promise<DiscoverRobotsResponse> {
  const { data } = await http.get<DiscoverRobotsResponse>("/api/robots/discover");
  return data;
}

export async function getStatus(root: string): Promise<StatusResponse> {
  const { data } = await http.get<StatusResponse>("/api/status", { params: { root } });
  return data;
}

export async function getFiles(root: string, subpath?: string): Promise<FileItem[]> {
  const { data } = await http.get<FileItem[]>("/api/files", { params: { root, subpath } });
  return data;
}

export async function getJobs(): Promise<Job[]> {
  const { data } = await http.get<Job[]>("/api/upload/jobs");
  return data;
}

// The durable view: a flat per-file log derived from the on-disk
// manifests, so it survives an agent restart (unlike getJobs()
// above, which read the in-memory JobStore). `root` defaults server-side to
// BASE_ROOT when omitted.
export async function getHistory(root?: string, limit?: number): Promise<HistoryResponse> {
  const { data } = await http.get<HistoryResponse>("/api/upload/history", { params: { root, limit } });
  return data;
}

export async function startUpload(root: string, files: string[], options?: UploadOptions): Promise<{ job_id: string }> {
  const { data } = await http.post<{ job_id: string }>("/api/upload/start", { root, files, options });
  return data;
}

export async function resumeAll(root: string): Promise<ActionResult> {
  const { data } = await http.post<ActionResult>("/api/upload/resume-all", { root });
  return data;
}

export async function retryFailed(root: string): Promise<ActionResult> {
  const { data } = await http.post<ActionResult>("/api/upload/retry-failed", { root });
  return data;
}

export async function checkExists(paths: string[]): Promise<{ missing: string[] }> {
  const { data } = await http.post<{ missing: string[] }>("/api/exists", { paths });
  return data;
}
