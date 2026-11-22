import type { RecordingEvidence } from "../lib/api";
import { formatBytes, formatIsoTimestamp } from "../lib/format";
import { uploadErrorText } from "../lib/recordingStatus";

export function RecordingDetails({ recording, path }: { recording: RecordingEvidence; path: string }) {
  const error = uploadErrorText(recording.last_upload_error);
  if (!error && !recording.upload_updated_at && !recording.server_verified_at && !recording.server_storage_path && !recording.saved_bytes && recording.resume_supported == null) return null;
  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer" aria-label={`Upload details for ${path}`}>Upload details</summary>
      {error ? <p className="mt-1 break-words text-danger-foreground">Failure reason: {error}</p> : null}
      {recording.saved_bytes != null && recording.saved_bytes > 0 ? (
        <p className="mt-1 text-foreground">Server saved at last check: {formatBytes(recording.saved_bytes)}. This is separate from bytes sent in the current attempt.</p>
      ) : null}
      {recording.resume_supported === true ? <p className="mt-1 text-foreground">Retries reuse saved parts while the server session remains available.</p> : null}
      {recording.upload_updated_at ? (
        <p className="mt-1 text-foreground">Last upload update: {formatIsoTimestamp(recording.upload_updated_at)}</p>
      ) : null}
      {recording.server_verified_at ? (
        <p className="mt-1 text-foreground">Verified at upload: {formatIsoTimestamp(recording.server_verified_at)}</p>
      ) : null}
      {recording.server_storage_path ? (
        <p className="mt-1 break-all text-foreground">Server path recorded at upload: <code>{recording.server_storage_path}</code></p>
      ) : null}
    </details>
  );
}
