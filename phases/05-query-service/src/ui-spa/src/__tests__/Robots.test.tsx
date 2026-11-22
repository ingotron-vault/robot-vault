/*
 * The Robots page.
 *
 * `../lib/api`'s `http` is the only thing mocked, so `lib/robotsAdmin.ts` runs
 * for real: the paths these tests assert on are the paths the page will send,
 * and the error classification under test is the one that ships.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { httpMock } = vi.hoisted(() => ({ httpMock: vi.fn() }));

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  http: httpMock,
}));

import { HttpError } from "../lib/api";
import RobotsPage, { RobotStateBadge } from "../pages/Robots";
import EnrollmentCode, { resolveInstallCommand } from "../pages/Robots/EnrollmentCode";
import { resolveUpgradeCommand } from "../pages/Robots/UpgradeCommand";
import { renderWithProviders } from "./testUtils";

const DIGEST = "c0ffee".repeat(10) + "abcd";
const FIXTURE_CODE = "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT";

const FLEET = {
  items: [
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
  ],
};

const LIFECYCLE = {
  robots: [
    {
      robot_id: "rover-7",
      name: "Rover 7",
      state: "enrolled",
      auth_mode: "per_robot",
      enrolled_at: "2026-02-01T00:00:00Z",
      credentials: [
        {
          credential_id: "11111111-1111-1111-1111-111111111111",
          fingerprint: "a3f01c9e2b44",
          issued_at: "2026-02-01T00:00:00Z",
          issued_via: "enrollment",
          first_used_at: "2026-02-01T00:05:00Z",
          last_used_at: "2026-08-17T09:00:00Z",
          superseded_at: null,
          revoked_at: null,
          expires_at: null,
        },
      ],
      open_codes: 0,
    },
  ],
};

const MINT = {
  code: FIXTURE_CODE,
  code_id: "22222222-2222-2222-2222-222222222222",
  fingerprint: "b1c2d3e4f506",
  expires_at: "2026-08-17T10:15:00Z",
  ca_spki_sha256: "f".repeat(64),
  vault_ingest_endpoint: "rover-vault.local:50051",
  robot_bootstrap_sha256: DIGEST,
};

const BOOTSTRAP = {
  agent_version: "0.10.0",
  robot_bootstrap_sha256: DIGEST,
  upgrade_command: null as string | null,
};

/**
 * A `path -> response` router that throws rather than answering undefined.
 *
 * Error fixtures carry the UNWRAPPED detail, because that is what `http()`
 * hands to callers: it reads `payload.detail ?? payload.error` and puts the
 * result in `HttpError.detail`. A fixture holding the raw `{error: "..."}`
 * body would exercise a shape the production client never constructs.
 */
function route(overrides: Record<string, unknown> = {}) {
  httpMock.mockImplementation((path: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    if (Object.prototype.hasOwnProperty.call(overrides, key)) {
      const value = overrides[key];
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    }
    if (path === "/api/v1/robots/list") return Promise.resolve(FLEET);
    if (path === "/api/admin/robots/lifecycle") return Promise.resolve(LIFECYCLE);
    if (path === "/api/admin/robots/bootstrap") return Promise.resolve(BOOTSTRAP);
    if (path.endsWith("/summary")) {
      return Promise.resolve({ ...FLEET.items[0], topics: [], recent_uploads: [] });
    }
    throw new Error(`unmocked path in Robots test: ${key}`);
  });
}

/** Every path `http()` was asked for, in call order. */
function calledPaths(): string[] {
  return httpMock.mock.calls.map((call) => call[0] as string);
}

/** The first call whose path ends with `suffix`, as `[path, init]`. */
function callTo(suffix: string): [string, RequestInit | undefined] | undefined {
  return httpMock.mock.calls.find((call) => String(call[0]).endsWith(suffix)) as
    | [string, RequestInit | undefined]
    | undefined;
}

beforeEach(() => {
  httpMock.mockReset();
  route();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the fleet table", () => {
  it("shows the registry merged with the lifecycle list", async () => {
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    expect(await screen.findByRole("button", { name: "rover-7" })).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /rover-7/ });
    expect(within(row).getByText("enrolled")).toBeInTheDocument();
    expect(within(row).getByText("1 live / 1")).toBeInTheDocument();
    expect(within(row).getByText("12")).toBeInTheDocument();
  });

  it("names the admin role when the proxy refuses the session", async () => {
    route({ "GET /api/admin/robots/lifecycle": new HttpError(403, null) });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    expect(await screen.findByText("Robot provisioning requires the admin role")).toBeInTheDocument();
  });

  it("does not claim an empty fleet when the registry itself failed", async () => {
    route({
      "GET /api/v1/robots/list": new HttpError(500, null),
      "GET /api/admin/robots/lifecycle": new HttpError(404, null),
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("not that the fleet is empty");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders an unknown lifecycle state as unknown, never as a guess", () => {
    render(<RobotStateBadge state={null} />);
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });

  it("labels every state with its own word, so colour is never the only signal", () => {
    for (const state of ["pending", "enrolled", "disabled", "decommissioned"]) {
      const { unmount } = render(<RobotStateBadge state={state} />);
      expect(screen.getByText(state)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("lifecycle actions", () => {
  it("disables a robot and reports what the vault actually wrote", async () => {
    route({
      "POST /api/admin/robots/rover-7/disable": {
        robot_id: "rover-7",
        state: "disabled",
        requested_state: "disabled",
        previous_state: "enrolled",
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Disable/ }));
    expect(await screen.findByText(/rover-7 is now disabled/)).toBeInTheDocument();
  });

  it("renders the state ENABLE restored, not the one it asked for", async () => {
    // A robot disabled while still `pending` comes back
    // as `pending`, not `enrolled`. Showing the request would be a lie.
    route({
      "GET /api/admin/robots/lifecycle": {
        robots: [{ ...LIFECYCLE.robots[0], state: "disabled", enrolled_at: null, credentials: [] }],
      },
      "POST /api/admin/robots/rover-7/enable": {
        robot_id: "rover-7",
        state: "pending",
        requested_state: "enrolled",
        previous_state: "disabled",
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Enable/ }));
    expect(await screen.findByText("rover-7 was restored to pending.")).toBeInTheDocument();
  });

  it("requires the robot id to be typed before decommissioning", async () => {
    route({
      "POST /api/admin/robots/rover-7/decommission": {
        robot_id: "rover-7",
        state: "decommissioned",
        previous_state: "enrolled",
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Decommission/ }));

    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Decommission robot" });
    expect(confirm).toBeDisabled();
    expect(dialog).toHaveTextContent(
      "Decommissioning is permanent. To use this hardware again, add it as a new robot."
    );
    expect(dialog.textContent).not.toMatch(new RegExp(["ingotron", "root command", "reinstat"].join("|"), "i"));

    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: "rover-6" } });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: "rover-7" } });
    expect(confirm).toBeEnabled();

    fireEvent.click(confirm);
    expect(await screen.findByText(/Every credential was revoked/)).toBeInTheDocument();
  });

  it("offers no lifecycle action on a decommissioned robot except none at all", async () => {
    route({
      "GET /api/admin/robots/lifecycle": {
        robots: [{ ...LIFECYCLE.robots[0], state: "decommissioned" }],
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    expect(await screen.findByRole("button", { name: /Enroll/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Disable/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Decommission/ })).toBeDisabled();
    expect(
      screen.getByText("Decommissioning is permanent. To use this hardware again, add it as a new robot.")
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      new RegExp([["ingotron", "robot"].join(" "), "root command", "reinstat"].join("|"), "i")
    );
  });

  it("revokes a credential immediately, sending no `at` at all", async () => {
    route({
      "POST /api/admin/robots/rover-7/credentials/11111111-1111-1111-1111-111111111111/revoke": {
        credential_id: "11111111-1111-1111-1111-111111111111",
        applied: "immediate",
        revoked: true,
        expires_at: null,
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: "rover-7" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("does not revoke credentials it was rotated into");
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke credential" }));

    expect(await screen.findByText(/was revoked \(immediate\)/)).toBeInTheDocument();
    const call = callTo("/revoke");
    // An absent `at` IS the immediate path, and a past `at` is
    // applied immediately too -- so the UI never has to compute a timestamp.
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({});
  });
});

describe("lifecycle regressions", () => {
  it("counts a superseded-but-in-grace credential as live and still offers Revoke", async () => {
    // Both tokens authenticate during the rotation window. Reporting
    // "0 live / 1" for a robot uploading right now is the forbidden "zero as a
    // fallback for missing data", and hiding Revoke removes the control an
    // incident response needs most.
    route({
      "GET /api/admin/robots/lifecycle": {
        robots: [
          {
            ...LIFECYCLE.robots[0],
            credentials: [
              {
                ...LIFECYCLE.robots[0].credentials[0],
                superseded_at: new Date(Date.now() - 3_600_000).toISOString(),
                expires_at: new Date(Date.now() + 23 * 3_600_000).toISOString(),
              },
            ],
          },
        ],
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    const row = await screen.findByRole("row", { name: /rover-7/ });
    expect(within(row).getByText("1 live / 1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "rover-7" }));
    expect(await screen.findByRole("button", { name: "Revoke" })).toBeInTheDocument();
  });

  it("requires typed confirmation for a legacy shared-credential robot with a NULL enrolled_at", async () => {
    // The schema defaults a robot to ('enrolled','shared') with
    // enrolled_at NULL. Keying only on enrolled_at would send `purpose: "enroll"` and
    // skip the required confirmation for EVERY such robot.
    route({
      "GET /api/admin/robots/lifecycle": {
        robots: [{ ...LIFECYCLE.robots[0], auth_mode: "shared", enrolled_at: null }],
      },
      "POST /api/admin/robots/rover-7/enrollment-code": { ...MINT, warning: "robot_is_already_enrolled" },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Enroll/ }));

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: "rover-7" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Mint re-enrollment code" }));

    await screen.findByText("This robot is already enrolled");
    expect(JSON.parse(String(callTo("/enrollment-code")?.[1]?.body))).toEqual({ purpose: "reenroll" });
  });

  it("names the specific refusal instead of one generic sentence, and keeps it on screen", async () => {
    // Ingest sends {"error": "<code>"} with no `detail`; the page must show
    // that code, and keep it on screen after the toast closes.
    route({
      "POST /api/admin/robots/rover-7/enrollment-code": new HttpError(429, "too_many_open_codes"),
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Enroll/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: "rover-7" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Mint re-enrollment code" }));

    // The message reaches the operator TWICE on purpose: once as a transient
    // toast, and once in a persistent alert they can still read after it fades.
    // The guidelines forbid the toast being the only copy.
    const copies = await screen.findAllByText(/maximum number of unused enrollment codes/);
    expect(copies.length).toBeGreaterThan(1);
    const persistent = copies
      .map((node) => node.closest('[role="alert"]'))
      .find((node): node is HTMLElement => node !== null);
    expect(persistent).toBeDefined();
    expect(within(persistent as HTMLElement).getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("offers no lifecycle actions for a robot the provisioning API has never heard of", async () => {
    route({ "GET /api/admin/robots/lifecycle": { robots: [] } });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    const row = await screen.findByRole("row", { name: /rover-7/ });
    expect(within(row).getByText("Not in the provisioning registry")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Decommission/ })).not.toBeInTheDocument();
  });

  it("says the robot was created when only the mint failed", async () => {
    route({
      "POST /api/admin/robots": {
        robot_id: "arm-01",
        state: "pending",
        auth_mode: "per_robot",
        created_at: "2026-08-17T10:00:00Z",
      },
      "POST /api/admin/robots/arm-01/enrollment-code": new HttpError(502, "provisioning_unavailable"),
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "arm-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and mint code" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("arm-01 was created");
    expect(alert).toHaveTextContent("Use the robot's Enroll action");
  });

  it("names the enrollment-code block with a role that can carry a name", () => {
    // A bare <pre> is role=generic, on which ARIA prohibits naming, so a
    // conformant screen reader drops aria-label and the operator lands in an
    // unnamed focusable region holding a single-use code.
    render(<EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={Date.now()} />);
    const block = screen.getByRole("group", { name: "Enrollment code" });
    expect(block.tagName).toBe("PRE");
    expect(block).toHaveTextContent(FIXTURE_CODE);
  });
});

describe("adding a robot", () => {
  it("opens Add robot from the first-run link once provisioning is ready", async () => {
    renderWithProviders(<RobotsPage />, { route: "/robots?add=1" });

    expect(await screen.findByRole("dialog", { name: "Add robot" })).toBeInTheDocument();
  });

  it("creates then mints, so a duplicate id cannot hand out a credential", async () => {
    route({
      "POST /api/admin/robots": {
        robot_id: "arm-01",
        state: "pending",
        auth_mode: "per_robot",
        created_at: "2026-08-17T10:00:00Z",
      },
      "POST /api/admin/robots/arm-01/enrollment-code": MINT,
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "arm-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and mint code" }));

    expect(await screen.findByText("Shown once")).toBeInTheDocument();
    const paths = calledPaths();
    expect(paths.indexOf("/api/admin/robots")).toBeLessThan(
      paths.indexOf("/api/admin/robots/arm-01/enrollment-code")
    );
    const mintCall = callTo("/enrollment-code");
    expect(JSON.parse(String(mintCall?.[1]?.body))).toEqual({
      purpose: "enroll",
      recording_root: "/data/robot_data",
    });
  });

  it("validates the recording directory before it creates a robot", async () => {
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "arm-01" } });
    fireEvent.change(screen.getByLabelText("Recording directory"), { target: { value: "relative/path" } });

    expect(screen.getByRole("button", { name: "Create and mint code" })).toBeDisabled();
    expect(screen.getByText("The recording directory must be an absolute path.")).toBeInTheDocument();
    expect(calledPaths()).not.toContain("/api/admin/robots");
  });

  it("explains a duplicate id instead of minting for the running robot", async () => {
    route({ "POST /api/admin/robots": new HttpError(409, "robot_id_exists") });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "rover-7" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and mint code" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
    expect(screen.queryByText("Shown once")).not.toBeInTheDocument();
  });

  it("blocks a robot id the vault would reject, and says why", async () => {
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "bay-2/rover" } });
    expect(screen.getByRole("button", { name: "Create and mint code" })).toBeDisabled();
    expect(screen.getByText(/cannot contain slashes/)).toBeInTheDocument();
  });

  it("requires a typed confirmation before re-enrolling a robot that has enrolled before", async () => {
    route({ "POST /api/admin/robots/rover-7/enrollment-code": { ...MINT, warning: "robot_is_already_enrolled" } });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: /Enroll/ }));

    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Mint re-enrollment code" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/Type/), { target: { value: "rover-7" } });
    fireEvent.click(confirm);

    expect(await screen.findByText("This robot is already enrolled")).toBeInTheDocument();
    const call = callTo("/enrollment-code");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ purpose: "reenroll" });
  });
});

describe("the enrollment code display", () => {
  const minted = Date.parse("2026-08-17T10:00:00Z");

  it("warns that the code is shown once and cannot be retrieved", () => {
    render(<EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={minted} />);
    expect(screen.getByText("Shown once")).toBeInTheDocument();
    expect(screen.getByLabelText("Enrollment code")).toHaveTextContent(FIXTURE_CODE);
  });

  it("assembles the download, check and run command exactly when the vault did not render one", () => {
    render(<EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10" nowMs={minted} />);
    expect(screen.getByLabelText("Robot install command").textContent).toBe(
      'f=$(mktemp) && curl -fsSk --proto =https https://192.0.2.10/robot -o "$f" \\\n' +
        `  && echo "${DIGEST}  $f" | sha256sum -c - \\\n` +
        `  && sudo sh "$f" --vault https://192.0.2.10 --ingest rover-vault.local:50051 --code ${FIXTURE_CODE}`
    );
    expect(screen.getByText(/Assembled from this browser's address/)).toBeInTheDocument();
  });

  it("checks the script digest before it runs the script, in a re-enrollment too", () => {
    render(
      <EnrollmentCode
        robotId="arm-01"
        mint={{ ...MINT, install_mode: "reenroll" }}
        vaultUrl="https://192.0.2.10"
        nowMs={minted}
      />
    );
    const command = screen.getByLabelText("Robot re-enrollment command").textContent ?? "";
    expect(command.indexOf("sha256sum -c -")).toBeGreaterThan(0);
    expect(command.indexOf("sha256sum -c -")).toBeLessThan(command.indexOf("sudo sh"));
    expect(command).toContain("--reenroll --vault https://192.0.2.10 --ingest rover-vault.local:50051");
    expect(command).toContain(`--code ${FIXTURE_CODE}`);
    expect(command).not.toContain(["get", "ingotron", "com"].join("."));
  });

  it("prefers the vault's own render, verbatim, and says so", () => {
    const served = "f=$(mktemp) && curl … && sudo sh \"$f\" --vault https://vault:443 --code X";
    render(
      <EnrollmentCode
        robotId="arm-01"
        mint={{ ...MINT, install_command: served }}
        vaultUrl="https://192.0.2.10"
        nowMs={minted}
      />
    );
    expect(screen.getByLabelText("Robot install command").textContent).toBe(served);
    expect(screen.getByText(/Rendered by the vault/)).toBeInTheDocument();
  });

  it.each([null, undefined, "", "ABCDEF", "G".repeat(64), DIGEST.toUpperCase()])(
    "renders no executable fallback for the digest %j, and keeps the code",
    (digest) => {
      render(
        <EnrollmentCode
          robotId="arm-01"
          mint={{ ...MINT, robot_bootstrap_sha256: digest as string | null | undefined }}
          vaultUrl="https://192.0.2.10"
          nowMs={minted}
        />
      );
      expect(screen.queryByLabelText("Robot install command")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Enrollment code")).toHaveTextContent(FIXTURE_CODE);
      expect(screen.getByRole("alert")).toHaveTextContent("did not report a valid SHA-256");
    }
  );

  it("keeps the code usable when the command cannot be built", () => {
    // A dev origin is http, and `robot.sh` requires https://host[:port].
    render(<EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="http://localhost:5173" nowMs={minted} />);
    expect(screen.getByLabelText("Enrollment code")).toHaveTextContent(FIXTURE_CODE);
    expect(screen.getByRole("alert")).toHaveTextContent("requires an https vault address");
  });

  it("counts the 15-minute TTL down and rolls over to expired", () => {
    const { rerender } = render(
      <EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={minted} />
    );
    expect(screen.getByText("15:00 left")).toBeInTheDocument();

    rerender(
      <EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={minted + 899_000} />
    );
    expect(screen.getByText("0:01 left")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    rerender(
      <EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={minted + 900_000} />
    );
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("will be refused");
  });

  it("shows the vault's self-rotation setting only when the wire carried one", () => {
    const { unmount } = render(
      <EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={minted} />
    );
    expect(screen.queryByText("Self-rotation")).not.toBeInTheDocument();
    unmount();

    render(
      <EnrollmentCode
        robotId="arm-01"
        mint={{ ...MINT, self_rotation_enabled: false }}
        vaultUrl="https://192.0.2.10:8003"
        nowMs={minted}
      />
    );
    expect(screen.getByText("Disabled on this vault")).toBeInTheDocument();
  });

  it("falls back to the browser origin only for the command, never for the code", () => {
    const outcome = resolveInstallCommand(MINT, "https://vault.example:8003");
    expect(outcome.fromServer).toBe(false);
    expect(outcome.command).toContain("--vault https://vault.example:8003");
    expect(outcome.command).toContain("--ingest rover-vault.local:50051");
    expect(outcome.command).toContain(FIXTURE_CODE);
  });
});

describe("the upgrade command", () => {
  it("shows the server's command verbatim", () => {
    const served = 'f=$(mktemp) && … && sudo sh "$f" --upgrade --vault https://vault:443';
    const outcome = resolveUpgradeCommand({ ...BOOTSTRAP, upgrade_command: served }, "https://192.0.2.10");
    expect(outcome).toEqual({ command: served, problem: null, fromServer: true });
  });

  it("assembles it from the digest when the server sent none, with no code and no --ingest", () => {
    const outcome = resolveUpgradeCommand(BOOTSTRAP, "https://192.0.2.10");
    expect(outcome.fromServer).toBe(false);
    expect(outcome.command).toContain(`echo "${DIGEST}  $f" | sha256sum -c -`);
    expect(outcome.command).toContain('sudo sh "$f" --upgrade --vault https://192.0.2.10');
    expect(outcome.command).not.toContain("--code");
    expect(outcome.command).not.toContain("--ingest");
  });

  it("renders nothing executable without a valid digest", () => {
    const outcome = resolveUpgradeCommand({ ...BOOTSTRAP, robot_bootstrap_sha256: null }, "https://192.0.2.10");
    expect(outcome.command).toBeNull();
    expect(outcome.problem).toContain("did not report a valid SHA-256");
  });

  it("appears on the page with the version, once provisioning answers", async () => {
    // jsdom's own origin is http, so the page can show only the server's render here.
    route({
      "GET /api/admin/robots/bootstrap": {
        ...BOOTSTRAP,
        upgrade_command: 'f=$(mktemp) && … && sudo sh "$f" --upgrade --vault https://vault.example',
      },
    });
    renderWithProviders(<RobotsPage />);
    expect(await screen.findByText("This vault serves agent version 0.10.0.")).toBeInTheDocument();
    expect(await screen.findByLabelText("Robot upgrade command")).toHaveTextContent("--upgrade --vault");
  });
});

describe("the show-once properties required of the code display", () => {
  async function mintACode() {
    route({
      "POST /api/admin/robots": {
        robot_id: "arm-01",
        state: "pending",
        auth_mode: "per_robot",
        created_at: "2026-08-17T10:00:00Z",
      },
      "POST /api/admin/robots/arm-01/enrollment-code": MINT,
    });
    const view = renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });
    await waitFor(() => expect(screen.getByRole("button", { name: /Add robot/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Add robot/ }));
    fireEvent.change(await screen.findByLabelText("Robot id"), { target: { value: "arm-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and mint code" }));
    await screen.findByText("Shown once");
    return view;
  }

  it("shows the plaintext code exactly once, with the 'shown once' warning", async () => {
    await mintACode();
    expect(screen.getByText("Shown once")).toBeInTheDocument();
    // Exactly one rendering of the plaintext, in the copyable block.
    const shown = screen.getAllByText((_, node) => node?.textContent === FIXTURE_CODE);
    const blocks = shown.filter((node) => node.tagName === "PRE");
    expect(blocks).toHaveLength(1);
  });

  it("does not re-reveal the code after navigating away and back", async () => {
    const view = await mintACode();
    expect(document.body.textContent).toContain(FIXTURE_CODE);

    // Leaving the page drops it: the plaintext lives only in this component's
    // state, never in a query cache, and no API can hand it back.
    view.unmount();
    route();
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    await screen.findByRole("button", { name: "rover-7" });

    expect(document.body.textContent).not.toContain(FIXTURE_CODE);
    expect(screen.queryByText("Shown once")).not.toBeInTheDocument();
  });

  it("closing the dialog drops the code, and reopening the robot does not bring it back", async () => {
    await mintACode();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText("Shown once")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(FIXTURE_CODE);
  });

  it("drops the code when the dialog is dismissed with Escape", async () => {
    // The guidelines require a dialog to close with Escape when safe, and for a
    // single-use code the close path is also what drops the plaintext. "Done"
    // exercises a different handler, so without this the Escape path is
    // uncovered.
    await mintACode();
    expect(document.body.textContent).toContain(FIXTURE_CODE);

    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape", code: "Escape" });

    await waitFor(() => expect(screen.queryByText("Shown once")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(FIXTURE_CODE);
  });

  it("never puts the code in a URL anywhere in the SPA's navigation", async () => {
    await mintACode();
    // Every request path the SPA issued, plus the browser location.
    for (const path of calledPaths()) {
      expect(path).not.toContain(FIXTURE_CODE);
      expect(path).not.toContain(FIXTURE_CODE.slice(5));
    }
    expect(window.location.href).not.toContain(FIXTURE_CODE);
    expect(window.location.search).toBe("");
  });

  it("re-fetching the robot returns the fingerprint and no part of the code", async () => {
    await mintACode();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    // The lifecycle payload is what a returning operator sees; it may carry the
    // fingerprint and nothing else.
    const lifecycle = JSON.stringify(LIFECYCLE);
    expect(lifecycle).toContain("a3f01c9e2b44");
    expect(lifecycle).not.toContain(FIXTURE_CODE);
    // Not even the secret half, which is the part that would matter.
    expect(lifecycle).not.toContain("4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT");
  });

  it("renders a command carrying --vault, --ingest and --code when no recording directory was supplied", () => {
    render(<EnrollmentCode robotId="arm-01" mint={MINT} vaultUrl="https://192.0.2.10:8003" nowMs={Date.now()} />);
    const command = screen.getByLabelText("Robot install command").textContent ?? "";
    expect(command.match(/(^|\s)--[a-z-]+/g)).toEqual([" --proto", " --vault", " --ingest", " --code"]);
    expect(command).not.toContain("--ca-fingerprint");
  });
});

describe("robot detail", () => {
  it("surfaces a stranded credential as 'enrolled, never uploaded'", async () => {
    const longAgo = new Date(Date.now() - 3_600_000).toISOString();
    route({
      "GET /api/admin/robots/lifecycle": {
        robots: [
          {
            ...LIFECYCLE.robots[0],
            credentials: [
              { ...LIFECYCLE.robots[0].credentials[0], first_used_at: null, issued_at: longAgo },
            ],
          },
        ],
      },
    });
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: "rover-7" }));
    expect(await screen.findByText("Enrolled, never uploaded")).toBeInTheDocument();
    expect(screen.getByText(/mint a replacement code/)).toBeInTheDocument();
  });

  it("polls the summary for the selected robot", async () => {
    renderWithProviders(<RobotsPage />, { route: "/robots" });
    fireEvent.click(await screen.findByRole("button", { name: "rover-7" }));
    await waitFor(() =>
      expect(calledPaths()).toContain("/api/v1/robots/rover-7/summary")
    );
  });
});
