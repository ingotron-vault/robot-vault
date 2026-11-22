/*
 * The enrollment code, and the exactness of the command built around it.
 *
 * The command is pinned against a FIXED fixture pin and secret rather than a
 * generated one, because "assembled correctly" is not the property under test:
 * "byte-identical to the query service's render" is. A generated fixture would let a
 * wrong separator, a lost backslash or a stray trailing newline pass as long as
 * the generator and the assembler agreed.
 */
import { describe, expect, it } from "vitest";
import {
  buildInstallCommand,
  buildReenrollmentCommand,
  buildUpgradeCommand,
  ENROLLMENT_CODE_LENGTH,
  DEFAULT_RECORDING_ROOT,
  expiryReadout,
  formatClock,
  InstallCommandUnavailable,
  isKnownRobotState,
  MAX_RECORDING_ROOT_LENGTH,
  normalizeBootstrapDigest,
  normalizeIngestEndpoint,
  normalizeVaultUrl,
  normalizeRecordingRoot,
  parseEnrollmentCode,
  ROBOT_STATES,
} from "../lib/enrollment";

/** A fixed, valid enrollment code (test values only). */
const FIXTURE_PIN = "7Q2M4XKB9WMF7T3DZ8H1RN4KQ2";
const FIXTURE_SECRET = "4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT";  // gitleaks:allow (test value)
const FIXTURE_CODE = `ign1.${FIXTURE_PIN}.${FIXTURE_SECRET}`;
const FIXTURE_VAULT = "https://192.0.2.10";
const FIXTURE_INGEST = "192.0.2.10:50051";
const FIXTURE_SHA = "0123456789abcdef".repeat(4);
const BASE = { vaultUrl: FIXTURE_VAULT, code: FIXTURE_CODE, sha256: FIXTURE_SHA, ingestEndpoint: FIXTURE_INGEST };

describe("enrollment code format", () => {
  it("is 64 characters: ign1. + 26 + . + 32", () => {
    expect(FIXTURE_PIN).toHaveLength(26);
    expect(FIXTURE_SECRET).toHaveLength(32);
    expect(FIXTURE_CODE).toHaveLength(64);
    expect(ENROLLMENT_CODE_LENGTH).toBe(64);
  });

  it("parses the fixture into its pin and secret", () => {
    expect(parseEnrollmentCode(FIXTURE_CODE)).toEqual({ pin: FIXTURE_PIN, secret: FIXTURE_SECRET });
  });

  it("rejects everything the robot-side parser rejects", () => {
    expect(parseEnrollmentCode("")).toBeNull();
    expect(parseEnrollmentCode(`ign2.${FIXTURE_PIN}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ignc1.${FIXTURE_PIN}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${FIXTURE_PIN}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${FIXTURE_PIN}.${FIXTURE_SECRET}.extra`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${FIXTURE_PIN.slice(1)}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${FIXTURE_PIN}.${FIXTURE_SECRET.slice(1)}`)).toBeNull();
  });

  it("does not case-fold and does not repair I/L/O/U, because the vault does neither", () => {
    expect(parseEnrollmentCode(FIXTURE_CODE.toLowerCase())).toBeNull();
    expect(parseEnrollmentCode(`ign1.${"I".repeat(26)}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${"L".repeat(26)}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${"O".repeat(26)}.${FIXTURE_SECRET}`)).toBeNull();
    expect(parseEnrollmentCode(`ign1.${FIXTURE_PIN}.${"U".repeat(32)}`)).toBeNull();
  });
});

describe("the install command", () => {
  it("downloads the script from the vault, checks its digest, then runs it", () => {
    expect(buildInstallCommand(BASE)).toBe(
      'f=$(mktemp) && curl -fsSk --proto =https https://192.0.2.10/robot -o "$f" \\\n' +
        `  && echo "${FIXTURE_SHA}  $f" | sha256sum -c - \\\n` +
        `  && sudo sh "$f" --vault https://192.0.2.10 --ingest 192.0.2.10:50051 --code ${FIXTURE_CODE}`
    );
  });

  it("is never a pipe into a shell, and never fetches from an outside host", () => {
    const command = buildInstallCommand(BASE);
    expect(command).not.toMatch(/\|\s*(sudo\s+)?(ba)?sh\b/);
    expect(command).not.toContain(["get", "ingotron", "com"].join("."));
    expect(command.indexOf("sha256sum -c -")).toBeLessThan(command.indexOf("sudo sh"));
  });

  it("has no trailing newline that would self-execute on paste", () => {
    const command = buildInstallCommand(BASE);
    expect(command.split("\n")).toHaveLength(3);
    expect(command.endsWith("\n")).toBe(false);
  });

  it("keeps the whole 64-character code intact", () => {
    expect(buildInstallCommand(BASE)).toContain(FIXTURE_CODE);
  });

  it("builds the re-enrollment and upgrade forms on the same checked download", () => {
    expect(buildReenrollmentCommand(BASE)).toContain(
      `sudo sh "$f" --reenroll --vault https://192.0.2.10 --ingest 192.0.2.10:50051 --code ${FIXTURE_CODE}`
    );
    const upgrade = buildUpgradeCommand({ vaultUrl: FIXTURE_VAULT, sha256: FIXTURE_SHA });
    expect(upgrade).toContain(`echo "${FIXTURE_SHA}  $f" | sha256sum -c -`);
    expect(upgrade.endsWith('sudo sh "$f" --upgrade --vault https://192.0.2.10')).toBe(true);
  });

  it.each([null, undefined, "", "abc", FIXTURE_SHA.toUpperCase(), `${FIXTURE_SHA}0`, FIXTURE_SHA.slice(1)])(
    "refuses to render anything for the digest %j",
    (sha256) => {
      for (const build of [
        () => buildInstallCommand({ ...BASE, sha256 }),
        () => buildReenrollmentCommand({ ...BASE, sha256 }),
        () => buildUpgradeCommand({ vaultUrl: FIXTURE_VAULT, sha256 }),
      ]) {
        expect(build).toThrow(InstallCommandUnavailable);
      }
      expect(() => normalizeBootstrapDigest(sha256)).toThrow(InstallCommandUnavailable);
    }
  );

  it("refuses an ingest endpoint that is not host:port", () => {
    for (const bad of ["", "host", "host:0", "host:65536", "host:80; rm -rf /", "ho st:80", "$(id):80", "host:80/x"]) {
      expect(() => normalizeIngestEndpoint(bad)).toThrow(InstallCommandUnavailable);
    }
    expect(normalizeIngestEndpoint("ingest.example.com:50051")).toBe("ingest.example.com:50051");
    expect(normalizeIngestEndpoint("[::1]:50051")).toBe("[::1]:50051");
  });

  it("quotes a vault host that carries shell metacharacters, so none can run", () => {
    // `$`, `;`, `'` and backticks survive the URL parser inside a host name.
    const command = buildInstallCommand({ ...BASE, vaultUrl: "https://a$b;c'd`e.example" });
    expect(command).toContain("'https://a$b;c'\\''d`e.example/robot'");
    expect(command).toContain("--vault 'https://a$b;c'\\''d`e.example'");
  });

  it("adds a validated recording directory", () => {
    const command = buildInstallCommand({ ...BASE, recordingRoot: "/mnt/robot_recordings" });
    expect(command.endsWith("--root /mnt/robot_recordings")).toBe(true);
    expect(normalizeRecordingRoot(DEFAULT_RECORDING_ROOT)).toBe(DEFAULT_RECORDING_ROOT);
  });

  it("refuses a recording directory that robot.sh would reject", () => {
    for (const bad of [
      "",
      "relative/path",
      "/etc/recordings",
      "/data/../etc",
      "/data/$HOME",
      "/srv/recordings /etc",
      "/srv/recordings\t/etc",
      " /srv/recordings",
      "/srv/recordings ",
    ]) {
      expect(() => normalizeRecordingRoot(bad)).toThrow(InstallCommandUnavailable);
    }
  });

  it("enforces the query-service recording directory length limit", () => {
    const atLimit = `/${"a".repeat(MAX_RECORDING_ROOT_LENGTH - 1)}`;
    const overLimit = `/${"a".repeat(MAX_RECORDING_ROOT_LENGTH)}`;

    expect(normalizeRecordingRoot(atLimit)).toBe(atLimit);
    expect(() => normalizeRecordingRoot(overLimit)).toThrow(
      `The recording directory must be ${MAX_RECORDING_ROOT_LENGTH} characters or fewer.`
    );
  });

  it("refuses to render a command around a malformed code", () => {
    expect(() => buildInstallCommand({ ...BASE, code: "ign1.short" })).toThrow(
      InstallCommandUnavailable
    );
  });
});

describe("the vault address", () => {
  it("keeps the port when there is one and normalises to an origin", () => {
    expect(normalizeVaultUrl("https://192.0.2.10:8003")).toBe("https://192.0.2.10:8003");
    expect(normalizeVaultUrl("https://vault.local:8003/")).toBe("https://vault.local:8003");
    expect(normalizeVaultUrl("https://vault.local")).toBe("https://vault.local");
  });

  it("refuses anything robot.sh could not parse as https://host[:port]", () => {
    for (const bad of [
      "http://vault.local:8003",
      "vault.local:8003",
      "https://vault.local/ui",
      "https://vault.local:8003?x=1",
      "https://vault.local:8003#frag",
      "https://user:pass@vault.local",
      "",
    ]) {
      expect(() => normalizeVaultUrl(bad)).toThrow(InstallCommandUnavailable);
    }
  });

  it("says why, so the page can show it instead of a broken command", () => {
    try {
      normalizeVaultUrl("http://vault.local:8003");
      throw new Error("expected a refusal");
    } catch (error) {
      expect(error).toBeInstanceOf(InstallCommandUnavailable);
      expect((error as InstallCommandUnavailable).reason).toBe("invalid_vault_url");
    }
  });
});

describe("the 15-minute expiry clock", () => {
  const minted = Date.parse("2026-08-17T10:00:00Z");
  const expires = "2026-08-17T10:15:00Z";

  it("counts down from the default TTL", () => {
    expect(expiryReadout(expires, minted)).toEqual({
      tone: "fresh",
      remainingSeconds: 900,
      label: "15:00 left",
    });
  });

  it("rolls over second by second", () => {
    expect(expiryReadout(expires, minted + 1_000).label).toBe("14:59 left");
    expect(expiryReadout(expires, minted + 60_000).label).toBe("14:00 left");
    expect(expiryReadout(expires, minted + 831_000).label).toBe("1:09 left");
  });

  it("warns inside the last two minutes without yet calling it dead", () => {
    expect(expiryReadout(expires, minted + 779_000).tone).toBe("fresh");
    expect(expiryReadout(expires, minted + 780_000).tone).toBe("expiring");
    expect(expiryReadout(expires, minted + 899_000)).toEqual({
      tone: "expiring",
      remainingSeconds: 1,
      label: "0:01 left",
    });
  });

  it("rolls over to expired exactly at the deadline and stays there", () => {
    expect(expiryReadout(expires, minted + 900_000)).toEqual({
      tone: "expired",
      remainingSeconds: 0,
      label: "Expired",
    });
    expect(expiryReadout(expires, minted + 5_000_000).tone).toBe("expired");
  });

  it("says unknown, never expired, when the timestamp is unreadable", () => {
    // A live code shown as dead makes the operator mint another and burn this
    // one; the other direction costs a failed enrollment they can retry.
    expect(expiryReadout(null, minted).tone).toBe("unknown");
    expect(expiryReadout(undefined, minted).tone).toBe("unknown");
    expect(expiryReadout("", minted).tone).toBe("unknown");
    expect(expiryReadout("not-a-timestamp", minted).tone).toBe("unknown");
    expect(expiryReadout("not-a-timestamp", minted).remainingSeconds).toBeNull();
  });

  it("handles the 24-hour ceiling the mint allows", () => {
    expect(expiryReadout("2026-08-18T10:00:00Z", minted).label).toBe("24:00:00 left");
    expect(formatClock(3600)).toBe("1:00:00");
    expect(formatClock(59)).toBe("0:59");
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(-9)).toBe("0:00");
  });
});

describe("robot states", () => {
  it("is the migration's CHECK constraint, spelled exactly", () => {
    expect(ROBOT_STATES).toEqual(["pending", "enrolled", "disabled", "decommissioned"]);
    for (const state of ROBOT_STATES) expect(isKnownRobotState(state)).toBe(true);
    expect(isKnownRobotState("decomissioned")).toBe(false);
    expect(isKnownRobotState("Enrolled")).toBe(false);
    expect(isKnownRobotState("revoked")).toBe(false);
  });
});
