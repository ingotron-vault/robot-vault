export interface RecordingEvidence {
  status?: string;
  integrity_verified?: boolean | null;
  integrity_verification_basis?: string | null;
  upload_error?: string | null;
  upload_updated_at?: string | null;
  processing_status?: string | null;
  processing_error?: string | null;
  processing_attempts?: number | null;
  processing_started_at?: string | null;
  processing_completed_at?: string | null;
  processing_updated_at?: string | null;
  processing_lease_expires_at?: string | null;
}

export function verificationLabel(upload: RecordingEvidence): string {
  if (upload.integrity_verified === true && upload.integrity_verification_basis === "client_sha256") return "Verified · SHA-256 matched";
  if (upload.integrity_verified === false || upload.integrity_verification_basis === "unverified") return "Not verified";
  return "Unknown · verification evidence unavailable";
}

export function processingLabel(upload: RecordingEvidence): string {
  switch (upload.processing_status) {
    case "pending": return "Waiting for processing";
    case "processing": return "Processing";
    case "completed": return "Ready for queries";
    case "completed_empty": return "Processed · no queryable rows";
    case "failed": return "Processing failed";
    case "dead_letter": return "Processing stopped · retries exhausted";
    default: return "Unknown · processing evidence unavailable";
  }
}

export function recordingStatus(upload: RecordingEvidence): string {
  if (upload.status === "failed" || ["failed", "dead_letter"].includes(upload.processing_status || "")) return "Needs attention";
  if (upload.status === "uploading") return "Uploading";
  if (upload.processing_status === "completed") return "Ready";
  if (upload.processing_status === "completed_empty") return "Processed · no rows";
  if (upload.processing_status === "processing") return "Processing";
  if (upload.processing_status === "pending") return "Waiting for processing";
  if (upload.integrity_verified === true && upload.integrity_verification_basis === "client_sha256") return "Verified";
  if (upload.status === "completed") return "Received";
  return "Unknown";
}

export function recordingNextStep(upload: RecordingEvidence): string {
  if (upload.status === "failed") return "Check the upload error and the robot agent's connection, then retry from the robot agent.";
  if (upload.status === "uploading") return "Keep the robot copy. Check transfer progress in the robot agent if the connection drops.";
  if (["failed", "dead_letter"].includes(upload.processing_status || "")) return "Share this upload ID and processing error with the vault administrator to inspect processor logs.";
  if (upload.processing_status === "completed_empty") return "Check the recording's topics and message types. Processing finished without queryable rows.";
  if (upload.processing_status === "completed") return "Open the chart or download generated files in recording details.";
  if (upload.processing_status === "pending") return "Wait for a processor to pick up this recording. If it remains queued, ask the vault administrator to check processor health.";
  if (upload.processing_status === "processing") return "Processing is running. If updates stop, share the upload ID and last update with the vault administrator.";
  return "Refresh the status. If processing evidence remains unavailable, ask the vault administrator to check this upload ID.";
}
