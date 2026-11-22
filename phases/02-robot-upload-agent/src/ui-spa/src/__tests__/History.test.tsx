import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock(...) factories are hoisted above every other statement in this
// file, so anything the factory closes over must itself be created via
// vi.hoisted -- a plain top-level `const` here would still throw
// "Cannot access before initialization" when the factory runs.
const { ROOT, mockJobs, mockHistory } = vi.hoisted(() => {
  const ROOT = "/tmp/robot_data/rover-1";

  // GET /api/upload/jobs never returns a `root` (see api.ts's Job
  // interface), and a file's status is one of the canonical
  // pending/uploading/completed/failed values -- never "uploaded".
  const mockJobs = [
    {
      job_id: "job-recent",
      status: "running",
      started_at: 1_700_000_500,
      completed_at: null,
      files: [{ path: `${ROOT}/a.mcap`, bytes_sent: 100, total_bytes: 1000, status: "uploading", error: null }],
    },
    {
      job_id: "job-older",
      status: "completed",
      started_at: 1_700_000_000,
      completed_at: 1_700_000_100,
      files: [
        { path: `${ROOT}/y.mcap`, bytes_sent: 1000, total_bytes: 1000, status: "completed", error: null },
        { path: `${ROOT}/z.mcap`, bytes_sent: 0, total_bytes: 500, status: "failed", error: { status: "exception" } },
      ],
    },
  ];

  const mockHistory = {
    root: ROOT,
    files: [
      {
        path: `${ROOT}/old.mcap`,
        status: "completed",
        file_size_bytes: 2048,
        sha256_hash: "abc",
        upload_id: "u1",
        recorded_at: "2024-06-01T00:00:00+00:00",
        created_at: null,
        labels: null,
        data_file_present: true,
      },
      {
        path: `${ROOT}/gone.mcap`,
        status: "completed",
        file_size_bytes: 1024,
        sha256_hash: "def",
        upload_id: "u2",
        recorded_at: "2024-05-01T00:00:00+00:00",
        created_at: null,
        labels: null,
        data_file_present: false,
      },
    ],
    truncated: false,
  };

  return { ROOT, mockJobs, mockHistory };
});

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    discoverRobots: vi.fn().mockResolvedValue({ robots: [] }),
    getJobs: vi.fn().mockResolvedValue(mockJobs),
    getHistory: vi.fn().mockResolvedValue(mockHistory),
  };
});

import History from "../views/History";
import { getHistory } from "../lib/api";
import { renderWithProviders } from "./testUtils";

describe("History", () => {
  beforeEach(() => {
    localStorage.setItem("lastRobot", ROOT);
  });

  it("renders jobs newest-first and counts successes against the canonical 'completed' status", async () => {
    renderWithProviders(<History />);

    await screen.findByText("running");

    // Scoped to the "Live jobs" table specifically -- the durable history
    // table below it also renders "completed" pills, and an unscoped query
    // would match both.
    const jobsTable = screen.getAllByRole("table")[0];
    const rows = within(jobsTable).getAllByRole("row");
    const rowTexts = rows.map((r) => r.textContent || "");
    const recentIdx = rowTexts.findIndex((t) => t.includes("running"));
    const olderIdx = rowTexts.findIndex((t) => t.includes("completed"));
    expect(recentIdx).toBeGreaterThan(-1);
    // Newest-first: job-recent's row should precede job-older's in the DOM.
    expect(olderIdx).toBeGreaterThan(recentIdx);

    // job-older has 2 files, one "completed" (y.mcap) and one "failed"
    // (z.mcap). Before the fix, the Uploaded column compared against the
    // fifth-vocabulary spelling "uploaded" and always read 0. Keep this as
    // the regression guard for the canonical status contract documented in
    // the phase 02 technical guide's "Upload history" section.
    const cells = within(rows[olderIdx]).getAllByRole("cell");
    expect(cells[3]).toHaveTextContent("2"); // Files
    expect(cells[4]).toHaveTextContent("1"); // Uploaded
    expect(cells[5]).toHaveTextContent("1"); // Failed
  });

  it("keeps a retained completed job's verbatim failure reason expandable after completion", async () => {
    const user = userEvent.setup();
    renderWithProviders(<History />);

    await screen.findByText("running");
    const toggle = screen.getByRole("button", { name: "Toggle files for job job-older" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    expect(screen.getByText('{"status":"exception"}')).toBeInTheDocument();
    expect(screen.getByText(`${ROOT}/z.mcap`)).toBeInTheDocument();
  });

  it("wires the durable per-file history from GET /api/upload/history, separate from the live jobs view", async () => {
    renderWithProviders(<History />);

    await screen.findByText(`${ROOT}/old.mcap`);
    expect(screen.getByText(`${ROOT}/gone.mcap`)).toBeInTheDocument();
    expect(screen.getByText(`Upload history — ${ROOT}`)).toBeInTheDocument();

    const goneRow = screen.getByText(`${ROOT}/gone.mcap`).closest("tr") as HTMLElement;
    expect(within(goneRow).getByText("Local file missing")).toBeInTheDocument();

    const oldRow = screen.getByText(`${ROOT}/old.mcap`).closest("tr") as HTMLElement;
    expect(within(oldRow).getByText("On this robot")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `Toggle manifest details for ${ROOT}/old.mcap` }))
      .toHaveAttribute("aria-expanded", "false");

    expect(vi.mocked(getHistory)).toHaveBeenCalledWith(ROOT);
  });

  it("groups durable history by day and filters it client-side", async () => {
    const user = userEvent.setup();
    renderWithProviders(<History />);

    await screen.findByText(`${ROOT}/old.mcap`);
    // Compute the expected labels the same way the component renders them
    // (History.tsx uses the SYSTEM locale), so this test passes under any
    // runner locale — it failed under en-GB ("1 Jun 2024") while the
    // literal /Jun 1, 2024/ only matched en-US.
    const dayLabel = (iso: string) =>
      new Date(iso).toLocaleDateString(undefined, {
        weekday: "long",
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    expect(screen.getByText(dayLabel("2024-06-01T00:00:00+00:00"))).toBeInTheDocument();
    expect(screen.getByText(dayLabel("2024-05-01T00:00:00+00:00"))).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search history" }), "gone");
    expect(screen.getByText(`${ROOT}/gone.mcap`)).toBeInTheDocument();
    expect(screen.queryByText(`${ROOT}/old.mcap`)).not.toBeInTheDocument();
  });

  it("shows a saved failure reason after its live job has gone away", async () => {
    const user = userEvent.setup();
    vi.mocked(getHistory).mockResolvedValueOnce({ ...mockHistory, files: [{
      ...mockHistory.files[0], status: "failed",
      last_upload_error: { status: "UNAVAILABLE", detail: "Upload connection timed out" },
      upload_updated_at: "2026-09-05T10:00:00Z",
    }] });
    renderWithProviders(<History />);
    await screen.findByText(`${ROOT}/old.mcap`);
    expect(screen.getByText("Upload failed")).toBeInTheDocument();
    expect(screen.getByText(/Check the connection to the server, then retry/)).toBeInTheDocument();
    await user.click(screen.getByText("Upload details"));
    expect(screen.getByText(/Failure reason: Upload connection timed out/)).toBeVisible();
  });
});
