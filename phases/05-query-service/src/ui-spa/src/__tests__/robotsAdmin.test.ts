/*
 * The provisioning client's judgements, none of which involve a network.
 */
import { describe, expect, it } from "vitest";
import { HttpError } from "../lib/api";
import {
  classifyProvisioningError,
  credentialIsLive,
  hasEverEnrolled,
  isEnrolledButNeverUsed,
  NEVER_USED_GRACE_MS,
  ProvisioningError,
  type RobotCredential,
} from "../lib/robotsAdmin";
import { mergeRobots, robotIdProblem, type FleetRobot } from "../pages/Robots";

const NOW = Date.parse("2026-08-17T12:00:00Z");

function credential(over: Partial<RobotCredential> = {}): RobotCredential {
  return {
    credential_id: "11111111-1111-1111-1111-111111111111",
    fingerprint: "a3f01c9e2b44",
    issued_at: "2026-08-17T10:00:00Z",
    issued_via: "enrollment",
    first_used_at: "2026-08-17T10:01:00Z",
    last_used_at: "2026-08-17T11:00:00Z",
    superseded_at: null,
    revoked_at: null,
    expires_at: null,
    ...over,
  };
}

describe("classifying a provisioning failure", () => {
  it("reads a 404 as an unknown robot, not a missing API", () => {
    const failure = classifyProvisioningError(new HttpError(404, null));
    expect(failure.reason).toBe("rejected");
    expect(failure.message).toContain("does not know that robot");
  });

  it("names the role on 401 and 403", () => {
    expect(classifyProvisioningError(new HttpError(401, null)).reason).toBe("forbidden");
    expect(classifyProvisioningError(new HttpError(403, null)).reason).toBe("forbidden");
    expect(classifyProvisioningError(new HttpError(403, null)).message).toContain("admin role");
  });

  it("keeps 502 distinct: the proxy did not synthesize a success", () => {
    const failure = classifyProvisioningError(new HttpError(502, "provisioning_unavailable"));
    expect(failure.reason).toBe("upstream_unavailable");
    expect(failure.detail).toBe("provisioning_unavailable");
  });

  it("separates a refusal from an outage", () => {
    expect(classifyProvisioningError(new HttpError(409, null)).reason).toBe("conflict");
    expect(classifyProvisioningError(new HttpError(429, null)).reason).toBe("conflict");
    expect(classifyProvisioningError(new HttpError(400, null)).reason).toBe("rejected");
    expect(classifyProvisioningError(new HttpError(500, null)).reason).toBe("unknown");
  });

  it("does not re-wrap something it already classified", () => {
    const original = new ProvisioningError("rejected", 404, "gone");
    expect(classifyProvisioningError(original)).toBe(original);
  });

  it("survives a rejection that is not an HttpError at all", () => {
    expect(classifyProvisioningError(new TypeError("Failed to fetch")).reason).toBe("unknown");
    expect(classifyProvisioningError(undefined).reason).toBe("unknown");
  });
});

describe("credential liveness", () => {
  it("counts a fresh, unused-but-recent credential as live", () => {
    expect(credentialIsLive(credential(), NOW)).toBe(true);
  });

  it("stops counting a revoked credential", () => {
    expect(credentialIsLive(credential({ revoked_at: "2026-08-17T11:30:00Z" }), NOW)).toBe(false);
  });

  it("KEEPS counting a superseded credential inside its grace window", () => {
    // Both tokens authenticate during the rotation window.
    // The schema's `robot_credentials_supersede_expiry` CHECK holds
    // `expires_at = superseded_grace_until`, so the grace deadline is on the
    // wire and ingest's own predicate honours it. Counting this as dead would
    // make a mid-rotation robot read "0 live / 1" while it is uploading, and
    // hide Revoke on the one credential an incident response needs to reach.
    expect(
      credentialIsLive(
        credential({ superseded_at: "2026-08-17T11:30:00Z", expires_at: "2026-08-18T11:30:00Z" }),
        NOW
      )
    ).toBe(true);
  });

  it("stops counting a superseded credential once its grace has elapsed", () => {
    expect(
      credentialIsLive(
        credential({ superseded_at: "2026-08-16T11:30:00Z", expires_at: "2026-08-17T11:30:00Z" }),
        NOW
      )
    ).toBe(false);
  });

  it("honours expires_at in both directions and treats null as no expiry", () => {
    expect(credentialIsLive(credential({ expires_at: "2026-08-17T11:59:59Z" }), NOW)).toBe(false);
    expect(credentialIsLive(credential({ expires_at: "2026-08-17T12:00:01Z" }), NOW)).toBe(true);
    expect(credentialIsLive(credential({ expires_at: null }), NOW)).toBe(true);
    expect(credentialIsLive(credential({ expires_at: "nonsense" }), NOW)).toBe(true);
  });
});

describe("'enrolled, never uploaded' (the race that cannot be removed)", () => {
  it("stays quiet inside the ten-minute grace", () => {
    const fresh = credential({ first_used_at: null, issued_at: new Date(NOW - 60_000).toISOString() });
    expect(isEnrolledButNeverUsed(fresh, NOW)).toBe(false);
  });

  it("speaks up once the credential is older than the grace and still unused", () => {
    const stale = credential({
      first_used_at: null,
      issued_at: new Date(NOW - NEVER_USED_GRACE_MS - 1000).toISOString(),
    });
    expect(isEnrolledButNeverUsed(stale, NOW)).toBe(true);
  });

  it("does not fire exactly at the boundary", () => {
    const boundary = credential({
      first_used_at: null,
      issued_at: new Date(NOW - NEVER_USED_GRACE_MS).toISOString(),
    });
    expect(isEnrolledButNeverUsed(boundary, NOW)).toBe(false);
  });

  it("says nothing about a credential that has been used, or one already dead", () => {
    const used = credential({ issued_at: new Date(NOW - 3_600_000).toISOString() });
    expect(isEnrolledButNeverUsed(used, NOW)).toBe(false);
    const revoked = credential({
      first_used_at: null,
      revoked_at: "2026-08-17T11:00:00Z",
      issued_at: new Date(NOW - 3_600_000).toISOString(),
    });
    expect(isEnrolledButNeverUsed(revoked, NOW)).toBe(false);
  });
});

describe("ever_enrolled, which decides whether a mint is a re-enrollment", () => {
  const base = { auth_mode: "per_robot", enrolled_at: null, state: "pending" };

  it("is false for a robot that has never enrolled", () => {
    expect(hasEverEnrolled(base)).toBe(false);
    expect(hasEverEnrolled(null)).toBe(false);
  });

  it("is true once enrolled_at is set", () => {
    expect(hasEverEnrolled({ ...base, enrolled_at: "2026-02-01T00:00:00Z", state: "enrolled" })).toBe(true);
  });

  it("is true for a legacy shared-credential robot with a NULL enrolled_at", () => {
    // The schema defaults a robot to
    // ('enrolled','shared') with enrolled_at NULL, and
    // `robots_enrolled_at_check` exempts auth_mode='shared' so that row is
    // legal and permanent. Keying only on enrolled_at would make EVERY such
    // robot skip the required typed re-enrollment confirmation and send a
    // falsified `purpose: "enroll"` into the audit trail.
    expect(hasEverEnrolled({ auth_mode: "shared", enrolled_at: null, state: "enrolled" })).toBe(true);
  });

  it("is true for a robot sitting in the enrolled state by any route", () => {
    expect(hasEverEnrolled({ ...base, state: "enrolled" })).toBe(true);
  });
});

describe("merging the registry with the lifecycle list", () => {
  const fleet: FleetRobot[] = [
    {
      robot_id: "rover-7",
      display_name: "Rover 7",
      first_seen: "2026-01-01T00:00:00Z",
      last_upload_at: "2026-08-17T09:00:00Z",
      total_uploads: 12,
      completed_uploads: 12,
      failed_uploads: 0,
      uploading: 0,
      total_bytes: 100,
    },
  ];

  it("keeps a robot that has uploaded but has no lifecycle row", () => {
    const merged = mergeRobots(fleet, []);
    expect(merged).toHaveLength(1);
    expect(merged[0].lifecycle).toBeNull();
    expect(merged[0].state).toBeNull();
  });

  it("keeps a robot that was created but has never uploaded", () => {
    const merged = mergeRobots([], [
      {
        robot_id: "arm-01",
        name: "Arm 01",
        state: "pending",
        auth_mode: "per_robot",
        enrolled_at: null,
        credentials: [],
        open_codes: 1,
      },
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0].fleet).toBeNull();
    expect(merged[0].state).toBe("pending");
  });

  it("prefers the lifecycle state, which is the only source that has one", () => {
    const merged = mergeRobots(fleet, [
      {
        robot_id: "rover-7",
        name: "Rover Seven",
        state: "disabled",
        auth_mode: "per_robot",
        enrolled_at: "2026-02-01T00:00:00Z",
        credentials: [],
        open_codes: 0,
      },
    ]);
    expect(merged).toHaveLength(1);
    expect(merged[0].state).toBe("disabled");
    expect(merged[0].name).toBe("Rover Seven");
    expect(merged[0].fleet?.total_uploads).toBe(12);
  });

  it("uses a state the registry supplies once its list gains one", () => {
    const merged = mergeRobots([{ ...fleet[0], state: "enrolled" }], []);
    expect(merged[0].state).toBe("enrolled");
  });

  it("sorts by robot id so a poll cannot reorder the table under a cursor", () => {
    const merged = mergeRobots(
      [
        { ...fleet[0], robot_id: "zeta" },
        { ...fleet[0], robot_id: "alpha" },
      ],
      []
    );
    expect(merged.map((robot) => robot.robot_id)).toEqual(["alpha", "zeta"]);
  });

  it("tolerates either source being absent entirely", () => {
    expect(mergeRobots(undefined, undefined)).toEqual([]);
  });
});

describe("robot id validation, mirroring the ingest-side check", () => {
  it("accepts an ordinary id", () => {
    expect(robotIdProblem("rover-7")).toBeNull();
    expect(robotIdProblem("a".repeat(50))).toBeNull();
  });

  it("names each refusal the way the server does", () => {
    expect(robotIdProblem("")).toBe("empty");
    expect(robotIdProblem("   ")).toBe("empty");
    expect(robotIdProblem("a".repeat(51))).toBe("too_long");
    expect(robotIdProblem("unknown")).toBe("unknown_sentinel");
    expect(robotIdProblem("UNKNOWN")).toBe("unknown_sentinel");
    expect(robotIdProblem(".")).toBe("invalid_charset");
    expect(robotIdProblem("..")).toBe("invalid_charset");
    expect(robotIdProblem("bay-2/rover")).toBe("invalid_charset");
    expect(robotIdProblem("bay\\rover")).toBe("invalid_charset");
    expect(robotIdProblem(`rover${String.fromCharCode(9)}7`)).toBe("invalid_charset");
  });
});
