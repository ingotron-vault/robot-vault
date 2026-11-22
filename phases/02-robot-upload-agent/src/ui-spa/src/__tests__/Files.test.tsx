import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock(...) factories are hoisted above every other statement in this
// file, so anything the factory closes over must itself be created via
// vi.hoisted -- a plain top-level `const` here would still throw
// "Cannot access before initialization" when the factory runs.
const { ROOT, OTHER_ROOT, mockFiles, mockStatus, runningJobFor } = vi.hoisted(() => {
  const ROOT = "/tmp/robot_data/rover-1";
  const OTHER_ROOT = "/tmp/robot_data/rover-2";

  const mockFiles = [
    { path: `${ROOT}/a.mcap`, name: "a.mcap", size: 1024, mtime: 1_700_000_000, status: "pending" },
    { path: `${ROOT}/b.mcap`, name: "b.mcap", size: 2048, mtime: 1_700_000_100, status: "completed" },
    { path: `${ROOT}/c.mcap`, name: "c.mcap", size: 4096, mtime: 1_700_000_200, status: "failed" },
  ];

  const mockStatus = {
    root: ROOT,
    pending_count: 1,
    connected: true,
    server_reachable: true,
    server_host: "ingest.local",
    server_port: 50051,
    network_type: "wifi",
    network_interface: "wlan0",
    network_ssid: null,
    is_wifi: true,
    is_cellular: false,
    is_ethernet: false,
    connection_quality: "good",
    batch_strategy: { parallel_streams: 2, retry_multiplier: 1.5 },
    pending: [],
  };

  // A running job whose files sit under `root` -- GET /api/upload/jobs
  // carries no `root` field to filter on server-side, so RobotContext
  // matches a job to a root by file path prefix (see jobMatchesRoot).
  function runningJobFor(root: string, fileName = "x.mcap") {
    return {
      job_id: "job-1",
      status: "running",
      started_at: 1_700_000_000,
      completed_at: null,
      files: [{ path: `${root}/${fileName}`, bytes_sent: 0, total_bytes: 1000, status: "uploading", error: null }],
    };
  }

  return { ROOT, OTHER_ROOT, mockFiles, mockStatus, runningJobFor };
});

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    discoverRobots: vi.fn().mockResolvedValue({
      robots: [{ name: "rover-1", path: ROOT, mcap_count: 3, uploaded_count: 1, pending_count: 1, failed_count: 1, total_size_bytes: 7168, last_modified: 1_700_000_200 }],
    }),
    getJobs: vi.fn().mockResolvedValue([]),
    getStatus: vi.fn().mockResolvedValue(mockStatus),
    getFiles: vi.fn().mockResolvedValue(mockFiles),
    checkExists: vi.fn().mockResolvedValue({ missing: [] }),
    startUpload: vi.fn().mockResolvedValue({ job_id: "job-2" }),
  };
});

import Files from "../views/Files";
import { StatusStrip } from "../components/StatusStrip";
import { renderWithProviders } from "./testUtils";
import { getFiles, getJobs, startUpload } from "../lib/api";

describe("Files", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("lastRobot", ROOT);
    vi.mocked(getJobs).mockResolvedValue([]);
  });

  it("renders file rows and toggles selection via checkboxes", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Files />);

    await screen.findByText("a.mcap");
    expect(screen.getByText("b.mcap")).toBeInTheDocument();
    expect(screen.getByText("c.mcap")).toBeInTheDocument();

    const uploadButton = screen.getByRole("button", { name: /upload selected/i });
    expect(uploadButton).toHaveTextContent("Upload selected (0)");

    const row = screen.getByText("a.mcap").closest("tr") as HTMLElement;
    const checkbox = row.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    await user.click(checkbox);
    expect(checkbox.checked).toBe(true);
    await waitFor(() => expect(screen.getByRole("button", { name: /upload selected/i })).toHaveTextContent("Upload selected (1)"));

    await user.click(checkbox);
    await waitFor(() => expect(screen.getByRole("button", { name: /upload selected/i })).toHaveTextContent("Upload selected (0)"));
  });

  it("clears selected paths when the robot root changes before starting another upload", async () => {
    const user = userEvent.setup();
    const otherFile = { ...mockFiles[0], path: `${OTHER_ROOT}/other.mcap`, name: "other.mcap" };
    vi.mocked(getFiles).mockResolvedValueOnce(mockFiles).mockResolvedValueOnce([otherFile]);
    renderWithProviders(<><StatusStrip /><Files /></>);

    await user.click(await screen.findByRole("checkbox", { name: "Select a.mcap" }));
    expect(screen.getByRole("button", { name: "Upload selected (1)" })).toBeEnabled();
    const rootInput = screen.getByRole("textbox", { name: "Custom root" });
    await user.clear(rootInput);
    await user.type(rootInput, `${OTHER_ROOT}{Enter}`);

    await screen.findByText("other.mcap");
    expect(screen.getByRole("button", { name: "Upload selected (0)" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Select other.mcap" }));
    await user.click(screen.getByRole("button", { name: "Upload selected (1)" }));
    await waitFor(() => expect(startUpload).toHaveBeenLastCalledWith(
      OTHER_ROOT, [otherFile.path], { bandwidth_mbps: 50, compute_chunk_hashes: false },
    ));
  });

  it("shows historical server verification separately from completed local upload history", async () => {
    vi.mocked(getFiles).mockResolvedValueOnce([
      { ...mockFiles[0], status: "completed", server_verified: true, safe_to_remove_local: false,
        server_verified_at: "2026-09-05T10:00:00Z", server_storage_path: "recordings/verified.mcap" },
      mockFiles[1],
    ]);
    renderWithProviders(<Files />);
    const verifiedRow = (await screen.findByText("a.mcap")).closest("tr")!;
    const legacyRow = screen.getByText("b.mcap").closest("tr")!;
    expect(within(verifiedRow).getByText("Verified at upload")).toBeInTheDocument();
    expect(within(verifiedRow).getByText(/does not approve deletion/)).toBeInTheDocument();
    expect(within(legacyRow).getByText("Upload recorded")).toBeInTheDocument();
    expect(within(legacyRow).getByText("Server verification unavailable")).toBeInTheDocument();
    expect(within(legacyRow).getByText(/Keep the local copy/)).toBeInTheDocument();
  });

  it("shows durable failure details and an honest retry action", async () => {
    const user = userEvent.setup();
    vi.mocked(getFiles).mockResolvedValueOnce([{ ...mockFiles[2],
      last_upload_error: { status: "UNAVAILABLE", detail: "Connection lost during upload" },
      upload_updated_at: "2026-09-05T10:00:00Z",
      saved_bytes: 2048, resume_supported: true,
    }]);
    renderWithProviders(<Files />);
    await screen.findByText("c.mcap");
    expect(screen.getByText(/Check the connection to the server, then retry/)).toBeInTheDocument();
    await user.click(screen.getByText("Upload details"));
    expect(screen.getByText(/Failure reason: Connection lost during upload/)).toBeVisible();
    expect(screen.getByText(/Server saved at last check: 2 KB/)).toBeVisible();
    expect(screen.getByText(/Retries reuse saved parts while the server session remains available/)).toBeVisible();
    expect(screen.getByText(/Large uploads reuse parts the server has saved/)).toBeInTheDocument();
  });

  it("uses status filters and a static MCAP indicator instead of a fake extension control", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Files />);

    await screen.findByText("a.mcap");
    expect(screen.getByText(".mcap recordings only")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /extension/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "pending (1)" }));
    expect(screen.getByText("a.mcap")).toBeInTheDocument();
    expect(screen.queryByText("b.mcap")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload all pending (1)" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "completed (1)" }));
    expect(screen.getByRole("button", { name: "Upload all pending (0)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "All filtered" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "None filtered" })).toBeInTheDocument();
  });

  it("does not silently replace an empty selected root with the global root", async () => {
    vi.mocked(getFiles).mockResolvedValueOnce([]);
    renderWithProviders(<Files />);

    await waitFor(() => expect(vi.mocked(getFiles)).toHaveBeenCalledWith(ROOT));
    expect(vi.mocked(getFiles).mock.calls.every(([requestedRoot]) => requestedRoot === ROOT)).toBe(true);
    expect(screen.queryByText(/showing \/tmp\/robot_data instead/i)).not.toBeInTheDocument();
  });

  it("distinguishes a successful empty file list from a failed file read", async () => {
    vi.mocked(getFiles).mockRejectedValueOnce(new Error("agent unavailable"));
    renderWithProviders(<Files />);

    expect(await screen.findByText("Could not load recordings")).toBeInTheDocument();
    expect(screen.queryByText("No recordings under this robot root")).not.toBeInTheDocument();
  });

  it("teaches the mounted BASE_ROOT path only after a successful empty file list", async () => {
    vi.mocked(getFiles).mockResolvedValueOnce([]);
    renderWithProviders(<Files />);

    expect(await screen.findByText("No recordings under this robot root")).toBeInTheDocument();
    expect(screen.getByText(/ROBOT_DATA_HOST_DIR/)).toBeInTheDocument();
  });

  // An explicitly-selected robot (lastRobot in localStorage) must survive
  // reload even when the reattach-on-mount effect finds a running job on a
  // *different* root -- it must not clobber the selection.
  it("keeps the explicitly-selected robot root when another root has a running job", async () => {
    vi.mocked(getJobs).mockResolvedValue([runningJobFor(OTHER_ROOT)]);

    renderWithProviders(<Files />);

    await screen.findByText("a.mcap");

    await waitFor(() => {
      expect(vi.mocked(getFiles)).toHaveBeenCalledWith(ROOT);
    });
    expect(vi.mocked(getFiles)).not.toHaveBeenCalledWith(OTHER_ROOT);
  });

  // A running job whose files match the current root means the current
  // root already has a job running -- Upload must be disabled with an
  // explanatory tooltip to prevent a hidden second job.
  it("disables Upload with a tooltip when a job is already running for the current root", async () => {
    const user = userEvent.setup();
    vi.mocked(getJobs).mockResolvedValue([runningJobFor(ROOT)]);

    renderWithProviders(<Files />);

    const row = await screen.findByText("a.mcap");
    const checkbox = (row.closest("tr") as HTMLElement).querySelector("input[type=checkbox]") as HTMLInputElement;
    await user.click(checkbox);

    const uploadButton = await screen.findByRole("button", { name: /upload selected/i });
    await waitFor(() => expect(uploadButton).toBeDisabled());
    expect(uploadButton).toHaveAttribute("title", "An upload is already running for this robot");
  });

  it("keeps Upload enabled when no job is running for the current root", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Files />);

    const row = await screen.findByText("a.mcap");
    const checkbox = (row.closest("tr") as HTMLElement).querySelector("input[type=checkbox]") as HTMLInputElement;
    await user.click(checkbox);

    const uploadButton = await screen.findByRole("button", { name: /upload selected/i });
    await waitFor(() => expect(uploadButton).toBeEnabled());
    expect(uploadButton).toHaveAttribute(
      "title",
      "Starts one batch per click; repeat to drain remaining pending files."
    );
  });

  // The upload path does not honor a "Concurrency" option, so it must not
  // appear in the UI or the POST payload.
  it("no longer shows a Concurrency option and never sends it to startUpload", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Files />);

    const row = await screen.findByText("a.mcap");
    const checkbox = (row.closest("tr") as HTMLElement).querySelector("input[type=checkbox]") as HTMLInputElement;
    await user.click(checkbox);

    await user.click(screen.getByRole("button", { name: /options/i }));
    expect(screen.queryByText("Concurrency")).not.toBeInTheDocument();
    expect(screen.getByText("Bandwidth (Mbps)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /upload selected/i }));

    await waitFor(() => expect(vi.mocked(startUpload)).toHaveBeenCalled());
    const calls = vi.mocked(startUpload).mock.calls;
    const [, , options] = calls[calls.length - 1];
    expect(options).not.toHaveProperty("concurrency");
    expect(options).toEqual({ bandwidth_mbps: 50, compute_chunk_hashes: false });
  });

  // The bandwidth number input must clamp blank/negative entries to >=1
  // instead of sending Number("") === 0 or a negative value.
  it("clamps blank or negative bandwidth input to at least 1", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Files />);

    await screen.findByText("a.mcap");
    await user.click(screen.getByRole("button", { name: /options/i }));

    const label = screen.getByText("Bandwidth (Mbps)").closest("label") as HTMLElement;
    const input = label.querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("50");

    fireEvent.change(input, { target: { value: "" } });
    expect(input.value).toBe("1");

    fireEvent.change(input, { target: { value: "-5" } });
    expect(input.value).toBe("1");
  });
});
