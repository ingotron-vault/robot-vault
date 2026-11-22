import { describe, expect, it } from "vitest";
import { recordingStatus, uploadRecovery } from "../lib/recordingStatus";

describe("recording status guidance", () => {
  it("does not turn a legacy completed manifest into verification or deletion permission", () => {
    const result = recordingStatus({ status: "completed", data_file_present: true });
    expect(result.label).toBe("Upload recorded");
    expect(result.verification).toBe("Server verification unavailable");
    expect(result.nextAction).toContain("Keep the local copy");
  });

  it("describes a receipt as historical verification and still requires current retention checks", () => {
    const result = recordingStatus({ status: "completed", server_verified: true, safe_to_remove_local: false });
    expect(result.verification).toBe("Verified at upload");
    expect(result.nextAction).toContain("server availability and retention policy are checked");
    expect(result.nextAction).toContain("does not approve deletion");
  });

  it("does not treat an absent local file as proof of a safe server copy", () => {
    const result = recordingStatus({ status: "failed", data_file_present: false });
    expect(result.localPresent).toBe(false);
    expect(result.nextAction).toContain("Check the server recording before relying on its copy");
  });

  it("distinguishes current upload progress from an interrupted manifest after restart", () => {
    expect(recordingStatus({ status: "uploading" }, { active: true }).label).toBe("Uploading");
    expect(recordingStatus({ status: "uploading" }).nextAction).toContain("select it to retry");
  });

  it("explains why pending uploads cannot start when the server is unreachable", () => {
    expect(recordingStatus({ status: "pending" }, { serverReachable: false }).nextAction)
      .toContain("Restore the server connection");
  });

  it.each([
    ["UNAVAILABLE", "Check the connection"],
    ["DEADLINE_EXCEEDED", "Check the connection"],
    ["PERMISSION_DENIED", "enrollment and server access"],
    ["RESOURCE_EXHAUSTED", "server storage and upload limits"],
    ["exception", "Review the failure reason"],
  ])("gives a recovery action for %s without inventing a failure cause", (status, expected) => {
    expect(uploadRecovery({ status })).toContain(expected);
  });

  it("uses the retained gRPC code on exception failures", () => {
    expect(uploadRecovery({ status: "exception", code: "UNAVAILABLE", detail: "RPC failed" }))
      .toContain("Check the connection");
  });
});
