import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock(...) factories are hoisted above every other statement in this
// file, so anything the factory closes over must itself be created via
// vi.hoisted -- a plain top-level `const` here would still throw
// "Cannot access before initialization" when the factory runs.
const { ROOT, mockJob, mockFiles, makeStatus, getStatusMock } = vi.hoisted(() => {
  const ROOT = "/tmp/robot_data/rover-1";

  // GET /api/upload/jobs never returns a `root` -- RobotContext's
  // reattach-on-mount derives one from this file's directory instead (see
  // guessJobRoot in context/RobotContext.tsx), which is why `a.mcap` sits
  // directly under ROOT.
  const mockJob = {
    job_id: "job-1",
    status: "running",
    started_at: 1_700_000_000,
    completed_at: null,
    files: [{ path: `${ROOT}/a.mcap`, bytes_sent: 500, total_bytes: 1000, status: "uploading", error: null }],
  };

  // Pending/Uploaded/Failed/Total-size are derived client-side from these
  // (the SPA's old /api/overview call had no real backend route): 2
  // pending, 3 completed, 1 failed, 7 files x 1024 bytes = 7 KB.
  const mockFiles = [
    { path: `${ROOT}/a.mcap`, name: "a.mcap", size: 1024, mtime: 1, status: "uploading" },
    { path: `${ROOT}/b.mcap`, name: "b.mcap", size: 1024, mtime: 2, status: "completed" },
    { path: `${ROOT}/c.mcap`, name: "c.mcap", size: 1024, mtime: 3, status: "completed" },
    { path: `${ROOT}/d.mcap`, name: "d.mcap", size: 1024, mtime: 4, status: "completed" },
    { path: `${ROOT}/e.mcap`, name: "e.mcap", size: 1024, mtime: 5, status: "pending" },
    { path: `${ROOT}/f.mcap`, name: "f.mcap", size: 1024, mtime: 6, status: "pending" },
    { path: `${ROOT}/g.mcap`, name: "g.mcap", size: 1024, mtime: 7, status: "failed" },
  ];

  function makeStatus(serverReachable: boolean) {
    return {
      root: ROOT,
      pending_count: 2,
      connected: serverReachable,
      server_reachable: serverReachable,
      server_host: "ingest.local",
      server_port: 50051,
      network_type: "wifi",
      network_interface: "wlan0",
      network_ssid: "lab-wifi",
      is_wifi: true,
      is_cellular: false,
      is_ethernet: false,
      connection_quality: "good",
      batch_strategy: { parallel_streams: 2, retry_multiplier: 1.5 },
      pending: [],
    };
  }

  const getStatusMock = vi.fn().mockResolvedValue(makeStatus(true));

  return { ROOT, mockJob, mockFiles, makeStatus, getStatusMock };
});

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    discoverRobots: vi.fn().mockResolvedValue({ robots: [] }),
    getJobs: vi.fn().mockResolvedValue([mockJob]),
    getStatus: getStatusMock,
    getFiles: vi.fn().mockResolvedValue(mockFiles),
    resumeAll: vi.fn(),
    retryFailed: vi.fn().mockResolvedValue({ uploaded: [], errors: [] }),
  };
});

import Dashboard from "../views/Dashboard";
import { QueueTray } from "../components/QueueTray";
import { renderWithProviders } from "./testUtils";
import { getFiles, getJobs, retryFailed } from "../lib/api";

describe("Dashboard", () => {
  beforeEach(() => {
    getStatusMock.mockReset().mockResolvedValue(makeStatus(true));
    vi.mocked(getJobs).mockReset().mockResolvedValue([mockJob]);
    vi.mocked(getFiles).mockReset().mockResolvedValue(mockFiles);
    vi.mocked(retryFailed).mockReset().mockResolvedValue({ uploaded: [], errors: [] });
  });

  it("shows queue and local-recording KPIs derived from GET /api/files", async () => {
    renderWithProviders(<Dashboard />);

    await waitFor(() => {
      expect(within(screen.getByLabelText("Queue depth")).getByText("3")).toBeInTheDocument(); // pending + uploading
      expect(within(screen.getByLabelText("Local recordings")).getByText("7 KB")).toBeInTheDocument();
      expect(within(screen.getByLabelText("Local recordings")).getByText("3 completed · 1 failed")).toBeInTheDocument();
    });
  });

  it("shows per-file failure text, retry, and an honest idle queue state", async () => {
    const user = userEvent.setup();
    vi.mocked(getJobs).mockResolvedValue([
      {
        ...mockJob,
        files: [
          mockJob.files[0],
          {
            path: `${ROOT}/failed.mcap`,
            bytes_sent: 0,
            total_bytes: 500,
            status: "failed",
            error: { detail: "ingest rejected this recording" },
          },
        ],
      },
    ]);
    renderWithProviders(<QueueTray />);

    expect(await screen.findByText("ingest rejected this recording")).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(3); // aggregate + one per file

    await user.click(screen.getByRole("button", { name: "Retry failed under this root" }));
    await waitFor(() => expect(vi.mocked(retryFailed)).toHaveBeenCalledWith(ROOT));
  });

  it("renders a degraded Callout instead of treating an unreachable server as an empty fleet", async () => {
    getStatusMock.mockResolvedValue(makeStatus(false));
    renderWithProviders(<Dashboard />);

    expect(await screen.findByText("Central ingest is unavailable")).toBeInTheDocument();
  });

  it("keeps inventory KPI values unknown when the file read fails", async () => {
    vi.mocked(getFiles).mockRejectedValue(new Error("agent disconnected"));
    renderWithProviders(<Dashboard />);

    expect(await screen.findByText("Could not load the recording inventory")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Queue depth")).getByText("—")).toBeInTheDocument();
  });
});
