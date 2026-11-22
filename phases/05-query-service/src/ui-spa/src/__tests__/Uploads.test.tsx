import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "@robot-vault/ui-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLocation } from "react-router-dom";

const { httpMock, authorizedFetchMock } = vi.hoisted(() => ({
  httpMock: vi.fn(),
  authorizedFetchMock: vi.fn(),
}));

vi.mock("../lib/api", () => ({ http: httpMock }));
vi.mock("../lib/auth", () => ({ authorizedFetch: authorizedFetchMock }));

import { isoToDatetimeLocal } from "../pages/Uploads";
import Uploads from "../pages/Uploads";
import { renderWithProviders } from "./testUtils";
import { OrbitThemeProvider } from "../components/OrbitTheme";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="search">{location.search}</div>;
}

function currentParams() {
  return new URLSearchParams(screen.getByTestId("search").textContent ?? "");
}

function uploadRequests() {
  return httpMock.mock.calls.map(([path]) => String(path)).filter((path) => path.startsWith("/api/v1/uploads/list?"));
}

describe("upload filter parsing", () => {
  it("treats a malformed URL timestamp as an empty filter instead of crashing", () => {
    expect(isoToDatetimeLocal("not-a-date")).toBe("");
    // The input shows host-local time. A zone-less datetime-local string is
    // parsed as local time, so the round trip must return the same instant in
    // any host time zone.
    const utc = "2026-08-09T10:30:00Z";
    const local = isoToDatetimeLocal(utc);
    expect(local).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    expect(new Date(local).getTime()).toBe(Date.parse(utc));
  });
});

describe("Uploads controls", () => {
  beforeEach(() => {
    httpMock.mockReset();
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve(["robot-a"]);
      if (path.startsWith("/api/v1/uploads/list?")) {
        return Promise.resolve({
          items: [{
            upload_id: "upload-42",
            robot_id: "robot-a",
            storage_path: "raw/robot-a/upload-42.mcap",
            file_size_bytes: 200,
            bytes_received: 100,
            status: "uploading",
            completed_at: null,
          }],
          page: { returned: 1, total_count: 1, offset: 0, has_prev: false, has_next: false },
        });
      }
      throw new Error(`unmocked path in Uploads test: ${path}`);
    });
  });

  it("refreshes processing evidence in an open drawer", async () => {
    let processingStatus = "processing";
    const upload = { upload_id: "refresh-42", robot_id: "robot-a", status: "completed", storage_path: "raw/robot-a/refresh.mcap", file_size_bytes: 200, completed_at: 1788595200 };
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve(["robot-a"]);
      if (path.startsWith("/api/v1/uploads/list?")) return Promise.resolve({ items: [{ ...upload, processing_status: processingStatus }], page: { returned: 1, total_count: 1, offset: 0 } });
      if (path.endsWith("/detail")) return Promise.resolve({ ...upload, processing_status: processingStatus });
      if (path.endsWith("/files")) return Promise.resolve({ parquet_keys: {} });
      throw new Error(`unmocked path: ${path}`);
    });
    renderWithProviders(<Uploads />, { route: "/uploads" });
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const drawer = within(await screen.findByRole("dialog"));
    expect(await drawer.findByRole("heading", { name: "Processing" })).toBeInTheDocument();
    processingStatus = "completed";
    fireEvent.click(drawer.getByRole("button", { name: "Refresh recording" }));
    expect(await drawer.findByRole("heading", { name: "Ready" })).toBeInTheDocument();
  });

  it("keeps robot, date preset, sort, and row limit when resetting the offset", async () => {
    renderWithProviders(<><Uploads /><LocationProbe /></>, { route: "/uploads?offset=50&keep=yes" });
    await screen.findByRole("progressbar", { name: "Upload upload-42 progress" });
    fireEvent.click(screen.getByRole("button", { name: /robot-a.*visible capture/i }));
    await waitFor(() => expect(currentParams().get("robot_id")).toBe("robot-a"));
    expect(currentParams().get("offset")).toBe("0");
    fireEvent.click(screen.getByRole("button", { name: "24h" }));
    const afterRange = currentParams();
    expect(afterRange.get("since")).toBeTruthy();
    expect(afterRange.get("until")).toBeTruthy();
    fireEvent.change(screen.getByRole("combobox", { name: "Sort by" }), { target: { value: "file_size_bytes" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Rows" }), { target: { value: "25" } });
    await waitFor(() => expect(currentParams().get("limit")).toBe("25"));
    const params = currentParams();
    expect(params.get("robot_id")).toBe("robot-a");
    expect(params.get("since")).toBe(afterRange.get("since"));
    expect(params.get("until")).toBe(afterRange.get("until"));
    expect(params.get("order_by")).toBe("file_size_bytes");
    expect(params.get("offset")).toBe("0");
    expect(params.get("keep")).toBe("yes");
    await waitFor(() => {
      const requests = uploadRequests();
      const request = new URLSearchParams(requests[requests.length - 1]?.split("?")[1]);
      expect(request.get("robot_id")).toBe("robot-a");
      expect(request.get("since")).toBe(params.get("since"));
      expect(request.get("until")).toBe(params.get("until"));
      expect(request.get("order_by")).toBe("file_size_bytes");
      expect(request.get("limit")).toBe("25");
      expect(request.get("offset")).toBe("0");
    });
  });

});

describe("Uploads S3 discovery", () => {
  const emptyPage = { items: [], page: { returned: 0, total_count: 0, offset: 0, has_prev: false, has_next: false } };
  beforeEach(() => {
    httpMock.mockReset();
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/uploads/list?")) return Promise.resolve(emptyPage);
      if (path.startsWith("/api/v1/s3/list?")) return Promise.resolve({ items: [] });
      throw new Error(`unmocked path: ${path}`);
    });
  });

  it("settles an empty fleet once even after refresh and rerender, then allows a manual scan", async () => {
    renderWithProviders(<Uploads />, { route: "/uploads?since=2026-09-20T00%3A00%3A00Z" });
    await waitFor(() => expect(httpMock.mock.calls.filter(([path, init]) => path === "/api/robots" && init?.signal)).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /^Refresh$/ }));
    await waitFor(() => expect(uploadRequests().length).toBeGreaterThan(1));
    expect(httpMock.mock.calls.filter(([path, init]) => path === "/api/robots" && init?.signal)).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /Scan S3 for uploads/ }));
    await waitFor(() => expect(httpMock.mock.calls.filter(([path, init]) => path === "/api/robots" && init?.signal)).toHaveLength(2));
  });

  it("restarts an aborted StrictMode effect replay and then settles once", async () => {
    const since = "2026-09-20T00:00:00Z";
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } } });
    client.setQueryData(["uploads", "", "", "completed_at", "desc", "", true, 50, 0, since, ""], emptyPage);
    render(<StrictMode><OrbitThemeProvider><QueryClientProvider client={client}><MemoryRouter initialEntries={[`/uploads?since=${encodeURIComponent(since)}`]}><ToastProvider><Uploads /></ToastProvider></MemoryRouter></QueryClientProvider></OrbitThemeProvider></StrictMode>);
    const scans = () => httpMock.mock.calls.filter(([path, init]) => path === "/api/robots" && init?.signal);
    await waitFor(() => expect(scans()).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole("button", { name: /Scan S3 for uploads/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /^Refresh$/ }));
    await waitFor(() => expect(uploadRequests().length).toBeGreaterThan(0));
    expect(scans()).toHaveLength(2);
  });

  it("stops after two bounded pages when object lists are empty", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve(["robot-a"]);
      if (path.startsWith("/api/v1/uploads/list?")) return Promise.resolve(emptyPage);
      if (path.startsWith("/api/v1/s3/list?")) return Promise.resolve({ items: [], next_continuation_token: `page-${httpMock.mock.calls.filter(([p]) => String(p).startsWith("/api/v1/s3/list?")).length}` });
      throw new Error(`unmocked path: ${path}`);
    });
    renderWithProviders(<Uploads />, { route: "/uploads?since=2026-09-20T00%3A00%3A00Z" });
    await waitFor(() => expect(httpMock.mock.calls.filter(([path]) => String(path).startsWith("/api/v1/s3/list?"))).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: /^Refresh$/ }));
    await waitFor(() => expect(uploadRequests().length).toBeGreaterThan(1));
    expect(httpMock.mock.calls.filter(([path]) => String(path).startsWith("/api/v1/s3/list?"))).toHaveLength(2);
  });

  it("shows a scan failure and retries only when requested", async () => {
    let calls = 0;
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve(["robot-a"]);
      if (path.startsWith("/api/v1/uploads/list?")) return Promise.resolve(emptyPage);
      if (path.startsWith("/api/v1/s3/list?")) return ++calls === 1 ? Promise.reject(new Error("offline")) : Promise.resolve({ items: [] });
      throw new Error(`unmocked path: ${path}`);
    });
    renderWithProviders(<Uploads />, { route: "/uploads?since=2026-09-20T00%3A00%3A00Z" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not scan S3 uploads");
    fireEvent.click(screen.getByRole("button", { name: /^Refresh$/ }));
    await waitFor(() => expect(uploadRequests().length).toBeGreaterThan(1));
    expect(calls).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry S3 scan" }));
    await waitFor(() => expect(calls).toBe(2));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("discards a stale scan after the robot filter changes", async () => {
    let resolveOld!: (value: unknown) => void;
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/v1/edition") return Promise.resolve({ edition: "pro" });
      if (path === "/api/robots") return Promise.resolve(["robot-a", "robot-b"]);
      if (path.startsWith("/api/v1/uploads/list?")) return Promise.resolve(emptyPage);
      if (path.startsWith("/api/v1/s3/list?")) {
        const prefix = new URLSearchParams(path.split("?")[1]).get("prefix");
        if (prefix?.includes("robot-a")) return new Promise((resolve) => { resolveOld = resolve; });
        return Promise.resolve({ items: [] });
      }
      throw new Error(`unmocked path: ${path}`);
    });
    renderWithProviders(<><Uploads /><LocationProbe /></>, { route: "/uploads?robot_id=robot-a&since=2026-09-20T00%3A00%3A00Z" });
    await waitFor(() => expect(resolveOld).toBeTypeOf("function"));
    fireEvent.change(screen.getByRole("combobox", { name: "Robot" }), { target: { value: "robot-b" } });
    await waitFor(() => expect(currentParams().get("robot_id")).toBe("robot-b"));
    await waitFor(() => expect(httpMock.mock.calls.some(([path]) => String(path).includes("processed%2Fparquet%2Frobot-b%2F"))).toBe(true));
    resolveOld({ items: [{ key: "processed/parquet/robot-a/part-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.parquet", size: 1 }] });
    await waitFor(() => expect(screen.queryByText(/Found 1 uploads via S3/)).not.toBeInTheDocument());
  });
});

describe("recording lifecycle evidence", () => {
  it("keeps transfer receipt separate from verification and query readiness", async () => {
    const { RecordingStatusDetails } = await import("../pages/Uploads");
    render(<RecordingStatusDetails upload={{ upload_id: "received", robot_id: "robot-a", status: "completed" }} />);
    expect(screen.getByRole("heading", { name: "Received" })).toBeInTheDocument();
    expect(screen.getByText("Received by the vault")).toBeInTheDocument();
    expect(screen.getByText("Unknown · verification evidence unavailable")).toBeInTheDocument();
    expect(screen.getByText("Unknown · processing evidence unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Check the robot's local copy, current server storage, and retention policy/)).toBeInTheDocument();
    expect(screen.queryByText("Ready for queries")).not.toBeInTheDocument();
  });

  it("shows a verified upload's processing failure and a concrete next step", async () => {
    const { RecordingStatusDetails } = await import("../pages/Uploads");
    render(<RecordingStatusDetails upload={{
      upload_id: "processing-failed", robot_id: "robot-a", status: "completed",
      integrity_verified: true, integrity_verification_basis: "client_sha256",
      processing_status: "dead_letter", processing_error: "Unsupported message schema",
      processing_attempts: 3, processing_updated_at: "2026-09-05T08:30:00Z",
    }} />);
    expect(screen.getByRole("heading", { name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByText("Verified · SHA-256 matched")).toBeInTheDocument();
    expect(screen.getByText("Processing stopped · retries exhausted")).toBeInTheDocument();
    expect(screen.getByText(/Unsupported message schema/)).toBeInTheDocument();
    expect(screen.getByText(/vault administrator to inspect processor logs/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
  });

  it("distinguishes empty processing output from query readiness", async () => {
    const { RecordingStatusDetails } = await import("../pages/Uploads");
    render(<RecordingStatusDetails upload={{ upload_id: "empty", robot_id: "robot-a", status: "completed", processing_status: "completed_empty" }} />);
    expect(screen.getByRole("heading", { name: "Processed · no rows" })).toBeInTheDocument();
    expect(screen.getByText(/Check the recording's topics and message types/)).toBeInTheDocument();
    expect(screen.queryByText("Ready for queries")).not.toBeInTheDocument();
  });
});
