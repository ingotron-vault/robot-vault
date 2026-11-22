import type { RecordingEvidence } from "./api";

export function uploadErrorText(error?: Record<string, unknown> | null): string | null {
  if (!error) return null;
  return typeof error.detail === "string" ? error.detail : JSON.stringify(error);
}

export function uploadRecovery(error?: Record<string, unknown> | null): string {
  const code = String(error?.code ?? error?.status ?? "").toLowerCase();
  if (/unauthenticated|permission_denied/.test(code)) {
    return "Check this robot's enrollment and server access, then retry.";
  }
  if (/resource_exhausted/.test(code)) {
    return "Check server storage and upload limits, then retry.";
  }
  if (/unavailable|deadline_exceeded/.test(code)) {
    return "Check the connection to the server, then retry.";
  }
  return "Review the failure reason, fix the problem, then retry from Files.";
}

export function recordingStatus(
  recording: RecordingEvidence & { status: string },
  options: { active?: boolean; serverReachable?: boolean } = {},
) {
  const localPresent = recording.data_file_present !== false;
  const label = recording.status === "completed" ? "Upload recorded"
    : recording.status === "failed" ? "Upload failed"
      : recording.status === "uploading" ? options.active ? "Uploading" : "Upload interrupted"
        : recording.status === "pending" ? "Waiting to upload" : "Upload status unknown";
  const verification = recording.server_verified === true ? "Verified at upload"
    : recording.server_verified === false ? "Server verification failed"
      : "Server verification unavailable";
  let nextAction = "Select this recording to upload.";
  if (!localPresent) {
    nextAction = "Local file missing. Check the server recording before relying on its copy.";
  } else if (recording.server_verified === true) {
    nextAction = "Keep the local copy until server availability and retention policy are checked. This receipt alone does not approve deletion.";
  } else if (recording.status === "completed") {
    nextAction = "Keep the local copy. Check server verification, current storage and retention policy before deleting it.";
  } else if (recording.status === "failed") {
    nextAction = `Keep the local copy. ${uploadRecovery(recording.last_upload_error)}`;
  } else if (recording.status === "uploading") {
    nextAction = options.active
      ? "Keep the local copy until the server verifies it. See the queue for live progress."
      : "No active upload found. Keep the local copy and select it to retry.";
  } else if (options.serverReachable === false) {
    nextAction = "Keep the local copy. Restore the server connection, then start the upload.";
  }
  return { label, verification, nextAction, localPresent };
}
