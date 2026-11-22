import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useNavigate } from "react-router-dom";

vi.mock("../lib/api", () => ({ http: vi.fn() }));
vi.mock("../components/Chart", async () => {
  const { forwardRef } = await import("react");
  return {
    default: forwardRef(function MockChart({ option, onEvents, selectionAnnouncement, accessibility }: any, ref: any) {
    const summary = {
      legend: option.legend?.data,
      series: (option.series || []).map((series: any) => ({
        name: series.name,
        showSymbol: series.showSymbol,
        data: series.data,
      })),
    };
    return <div aria-label={accessibility?.title}>
        <button
          ref={ref}
          type="button"
          aria-label="Time series chart"
          data-chart-summary={JSON.stringify(summary)}
          onClick={() => onEvents?.click?.({
            seriesName: option.series?.[0]?.name,
            value: option.series?.[0]?.data?.[0],
          })}
        >chart</button>
        <span role="status">{selectionAnnouncement}</span>
      </div>;
    }),
  };
});

import Explore, { queryErrorMessage } from "../pages/Explore";
import { renderWithProviders } from "./testUtils";
import { http } from "../lib/api";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  httpMock.mockReset();
});

describe("Explore saved views", () => {
  it("preserves a saved view's filters when renaming it from a different selection", async () => {
    const savedView = {
      robot: "saved-robot", topic: "saved-topic", bucket: "hour",
      since: "2026-09-01T00:00:00Z", until: "2026-09-02T00:00:00Z",
    };
    localStorage.setItem("robot-vault:explore-saved-views", JSON.stringify({ Bench: savedView }));
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();
    renderWithProviders(<Explore />, { route: "/explore?robot=current-robot&bucket=day" });

    await user.click(screen.getByRole("button", { name: "Rename Bench" }));
    const name = screen.getByRole("textbox", { name: "Rename saved view" });
    await user.clear(name);
    await user.type(name, "Renamed bench");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(JSON.parse(localStorage.getItem("robot-vault:explore-saved-views") || "{}"))
      .toEqual({ "Renamed bench": savedView });
  });

  it("stores a structured local view and restores it through a saved-view chip", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();

    renderWithProviders(<Explore />, { route: "/explore?robot=rover-1&bucket=hour" });

    await user.click(screen.getByRole("button", { name: "Save view" }));
    await user.type(screen.getByRole("textbox", { name: "Saved view name" }), "Morning bench");
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(JSON.parse(localStorage.getItem("robot-vault:explore-saved-views") || "{}"))
      .toEqual({
        "Morning bench": {
          robot: "rover-1",
          topic: null,
          bucket: "hour",
          since: "",
          until: "",
        },
    });
    expect(screen.getByRole("button", { name: "Morning bench" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Saved Explore views" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent('Saved view "Morning bench".');
  });

  it("confirms overwrite and supports inline rename and delete", async () => {
    localStorage.setItem("robot-vault:explore-saved-views", JSON.stringify({
      Bench: { robot: "old", topic: null, bucket: "day", since: "", until: "" },
    }));
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();
    renderWithProviders(<Explore />, { route: "/explore?robot=new-robot" });

    await user.click(screen.getByRole("button", { name: "Save view" }));
    await user.type(screen.getByRole("textbox", { name: "Saved view name" }), "Bench");
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);
    await user.click(screen.getByRole("button", { name: "Overwrite view" }));
    expect(JSON.parse(localStorage.getItem("robot-vault:explore-saved-views") || "{}").Bench.robot).toBe("new-robot");

    await user.click(screen.getByRole("button", { name: "Rename Bench" }));
    const rename = screen.getByRole("textbox", { name: "Rename saved view" });
    await user.clear(rename);
    await user.type(rename, "Bench two");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(screen.getByRole("button", { name: "Bench two" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete Bench two" }));
    expect(screen.getByRole("button", { name: "Cancel delete Bench two" })).toHaveTextContent("Keep");
    await user.click(screen.getByRole("button", { name: "Cancel delete Bench two" }));
    expect(screen.getByRole("button", { name: "Delete Bench two" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete Bench two" }));
    await user.click(screen.getByRole("button", { name: "Confirm delete Bench two" }));
    expect(screen.queryByRole("button", { name: "Bench two" })).not.toBeInTheDocument();
  });

  it("drops malformed saved-view values instead of issuing invalid selections", () => {
    localStorage.setItem("robot-vault:explore-saved-views", JSON.stringify({
      "Bad bucket": { robot: "rover", topic: "twist", bucket: "century", since: "", until: "" },
      "Bad date": { robot: "rover", topic: "twist", bucket: "day", since: "not-a-date", until: "" },
    }));
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Explore />);

    expect(screen.queryByRole("group", { name: "Saved Explore views" })).not.toBeInTheDocument();
  });
});

describe("Explore SQL console", () => {
  it("explains the stable result-limit response instead of hiding it", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      if (path === "/api/v1/query/sql") {
        return Promise.reject({
          status: 422,
          detail: { code: "result_limit_exceeded", max_rows: 10_000 },
        });
      }
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();

    renderWithProviders(<Explore />);
    await user.click(screen.getByRole("button", { name: "SQL" }));
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(
      await screen.findByText(
        "Result exceeds the 10,000 row limit. Narrow the query and retry."
      )
    ).toBeInTheDocument();
    expect(screen.queryByText("Query failed")).not.toBeInTheDocument();
  });

  it("runs with the documented keyboard shortcut and restores Stream on back", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      if (path === "/api/v1/query/sql") return Promise.resolve({ columns: ["n"], rows: [[2]] });
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();
    function Harness() {
      const navigate = useNavigate();
      return <><Explore /><button onClick={() => navigate(-1)}>Browser back</button></>;
    }
    renderWithProviders(<Harness />);

    await user.click(screen.getByRole("button", { name: "SQL" }));
    const editor = screen.getByRole("textbox", { name: "SQL query" });
    editor.focus();
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(await screen.findByRole("status")).toHaveTextContent("Query returned 1 rows.");
    expect(httpMock).toHaveBeenCalledWith("/api/v1/query/sql", expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: "Browser back" }));
    expect(await screen.findByRole("heading", { name: "Select a stream" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stream" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the visible mode and stream selection synchronized with browser history", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["rover-1", "rover-2"]);
      if (path === "/api/v1/message-types?robot_id=rover-2") return Promise.resolve(["imu"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path.startsWith("/api/timeseries?")) return Promise.resolve({ points: [] });
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();
    function Harness() {
      const navigate = useNavigate();
      return <>
        <Explore />
        <button onClick={() => navigate("/explore?robot=rover-2&topic=imu&bucket=day")}>Open alternate stream</button>
        <button onClick={() => navigate("/explore?robot=rover-1&topic=twist&bucket=hour&mode=sql")}>Open initial SQL view</button>
        <button onClick={() => navigate(-1)}>Browser back</button>
      </>;
    }
    renderWithProviders(<Harness />, { route: "/explore?robot=rover-1&topic=twist&bucket=hour" });

    await user.click(screen.getByRole("button", { name: "Open alternate stream" }));
    await waitFor(() => {
      expect(screen.getByLabelText("Robot")).toHaveValue("rover-2");
      expect(screen.getByLabelText("Topic")).toHaveValue("imu");
      expect(screen.getByLabelText("Bucket")).toHaveValue("day");
    });

    await user.click(screen.getByRole("button", { name: "Open initial SQL view" }));
    expect(await screen.findByRole("button", { name: "SQL" })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Browser back" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Stream" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByLabelText("Robot")).toHaveValue("rover-2");
      expect(screen.getByLabelText("Topic")).toHaveValue("imu");
      expect(screen.getByLabelText("Bucket")).toHaveValue("day");
    });
  });

  it("reports copy-link success instead of failing silently", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });
    renderWithProviders(<Explore />);
    await user.click(screen.getByRole("button", { name: "Copy link" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Explore link copied.");
    expect(writeText).toHaveBeenCalledOnce();
  });

  it("announces failed utility actions as alerts", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });
    renderWithProviders(<Explore />);

    await user.click(screen.getByRole("button", { name: "Copy link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not copy the link");
  });

  it("ignores corrupted SQL history instead of crashing SQL mode", async () => {
    localStorage.setItem("explore_sql_history", JSON.stringify({ query: "SELECT 1" }));
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve([]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Explore />, { route: "/explore?mode=sql&topic=twist" });

    expect(await screen.findByText("No query history yet.")).toBeInTheDocument();
    expect(httpMock.mock.calls.some(([path]) => String(path).startsWith("/api/timeseries?"))).toBe(false);
  });

  it("pauses stream auto-refresh while SQL mode is active", async () => {
    const user = userEvent.setup();
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["rover-1", "rover-2"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path.startsWith("/api/timeseries?")) return Promise.resolve({ points: [] });
      throw new Error(`unmocked path in test: ${path}`);
    });
    renderWithProviders(<Explore />, { route: "/explore?robot=rover-1&topic=twist" });

    await screen.findByText("No points in the selected range.");
    await user.click(screen.getByRole("checkbox", { name: "Auto-refresh" }));
    await waitFor(() => expect(setIntervalSpy.mock.calls.some(([, delay]) => delay === 10_000)).toBe(true));

    const initialTimers = setIntervalSpy.mock.calls.filter(([, delay]) => delay === 10_000).length;
    await user.selectOptions(screen.getByLabelText("Robot"), "rover-2");
    await waitFor(() => {
      expect(clearIntervalSpy).toHaveBeenCalled();
      expect(setIntervalSpy.mock.calls.filter(([, delay]) => delay === 10_000).length).toBeGreaterThan(initialTimers);
    });

    await user.click(screen.getByRole("button", { name: "SQL" }));

    await waitFor(() => expect(clearIntervalSpy).toHaveBeenCalled());
  });
});

describe("Explore query admission feedback", () => {
  it.each([
    ["query_timeout", /timed out/i],
    ["query_capacity_exceeded", /query slots are busy/i],
    ["source_range_too_wide", /time range is too wide/i],
    ["source_range_reversed", /From must be earlier/i],
    ["source_plan_unbounded", /both From and Until/i],
    ["source_file_limit_exceeded", /too many source files/i],
    ["source_row_limit_exceeded", /too many source rows/i],
    ["source_byte_limit_exceeded", /too many source bytes/i],
    ["query_unavailable", /temporarily unavailable/i],
  ])("provides a concrete stream recovery step for %s", (code, expected) => {
    expect(queryErrorMessage({ detail: { code } }, "stream")).toMatch(expected);
  });

  it("directs SQL source-limit failures to the prunable Stream workspace", () => {
    const message = queryErrorMessage({ detail: { code: "source_row_limit_exceeded" } }, "sql");
    expect(message).toMatch(/Switch to Stream mode/i);
    expect(message).not.toMatch(/WHERE|timestamp filter/i);
  });
});

describe("Explore time series", () => {
  it("reports a robot-list outage and keeps manual refresh unavailable without a valid topic", async () => {
    const robotsFailure = new Error("registry unavailable");
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.reject(robotsFailure);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve([]);
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Explore />, { route: "/explore?topic=stale" });

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the robot list");
    expect(screen.getByRole("button", { name: "Retry robot list" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    expect(httpMock.mock.calls.some(([path]) => String(path).startsWith("/api/timeseries?"))).toBe(false);
  });

  it("drops a topic that is not valid for the newly selected robot", async () => {
    const user = userEvent.setup();
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["rover-1", "rover-2"]);
      if (path === "/api/v1/message-types?robot_id=rover-2") return Promise.resolve(["imu"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path.startsWith("/api/timeseries?")) return Promise.resolve({ points: [] });
      throw new Error(`unmocked path in test: ${path}`);
    });
    renderWithProviders(<Explore />, { route: "/explore?robot=rover-1&topic=twist" });

    await screen.findByText("No points in the selected range.");
    await user.selectOptions(screen.getByLabelText("Robot"), "rover-2");

    await waitFor(() => expect(screen.getByLabelText("Topic")).toHaveValue("imu"));
    expect(httpMock.mock.calls.some(([path]) => String(path).includes("robot_id=rover-2&topic=twist"))).toBe(false);
    expect(httpMock.mock.calls.some(([path]) => String(path).includes("robot_id=rover-2&topic=imu"))).toBe(true);
  });

  it("clamps recorded history to the server-advertised source window", async () => {
    const user = userEvent.setup();
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["rover-1"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path === "/api/v1/schema?robot_id=rover-1&topic=twist") {
        return Promise.resolve({ ts_min: "2026-01-01T00:00:22Z", ts_max: "2026-03-15T00:00:42Z" });
      }
      if (path === "/api/v1/query/limits") {
        return Promise.resolve({
          max_source_range_seconds: 2_678_400,
          max_source_files: 2_000,
          max_source_rows: 50_000_000,
          max_source_bytes: 10_000_000_000,
          query_timeout_seconds: 30,
          max_concurrent_queries: 4,
        });
      }
      if (path.startsWith("/api/timeseries?")) return Promise.resolve({ points: [] });
      throw new Error(`unmocked path in test: ${path}`);
    });
    renderWithProviders(<Explore />, { route: "/explore?robot=rover-1&topic=twist" });

    await user.click(screen.getByRole("button", { name: "All-time" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Loaded the latest 31 days instead");
    const from = screen.getByLabelText("From") as HTMLInputElement;
    const until = screen.getByLabelText("Until") as HTMLInputElement;
    expect(new Date(until.value).getTime() - new Date(from.value).getTime()).toBe(2_678_400_000);
    expect(httpMock).toHaveBeenCalledWith("/api/v1/query/limits");
    await waitFor(() => expect(httpMock.mock.calls.some(([path]) => String(path).includes("until=2026-03-15T00%3A01%3A00.000Z"))).toBe(true));
  });

  it("shows stable stream admission failures with an actionable alert", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["rover-1"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path.startsWith("/api/timeseries?")) return Promise.reject({ detail: { code: "source_file_limit_exceeded" } });
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Explore />, { route: "/explore?robot=rover-1&topic=twist" });

    expect(await screen.findByRole("alert")).toHaveTextContent("too many source files");
  });

  it("renders every typed JointState series and makes a point click inspectable", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["example-manipulator-01"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["joint_state"]);
      if (path.startsWith("/api/timeseries?")) {
        return Promise.resolve({
          points: [{ t: "2026-01-01T00:00:00", x: null, y: null, z: null, n: 400 }],
          columns: ["x", "y", "z"],
          all_null_columns: ["x", "y", "z"],
          series_source: "typed_joint_state",
          series: [
            {
              name: "left_elbow · position",
              field: "position",
              dimensions: { joint_name: "left_elbow" },
              points: [{ t: "2026-01-01T00:00:00", value: 0.9859, n: 400 }],
            },
            {
              name: "right_elbow · effort",
              field: "effort",
              dimensions: { joint_name: "right_elbow" },
              points: [{ t: "2026-01-01T00:00:00", value: -402.7, n: 200 }],
            },
          ],
        });
      }
      throw new Error(`unmocked path in test: ${path}`);
    });
    const user = userEvent.setup();

    renderWithProviders(<Explore />, {
      route: "/explore?robot=example-manipulator-01&topic=joint_state",
    });

    const chart = await screen.findByRole("button", { name: "Time series chart" });
    const summary = JSON.parse(chart.getAttribute("data-chart-summary") || "{}");
    expect(summary.legend).toEqual(["left_elbow · position", "right_elbow · effort"]);
    expect(summary.series).toEqual([
      {
        name: "left_elbow · position",
        showSymbol: true,
        data: [["2026-01-01T00:00:00", 0.9859]],
      },
      {
        name: "right_elbow · effort",
        showSymbol: true,
        data: [["2026-01-01T00:00:00", -402.7]],
      },
    ]);
    expect(screen.queryByText(/numeric fields .* are empty/i)).not.toBeInTheDocument();

    await user.click(chart);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Selected left_elbow · position at 2026-01-01T00:00:00: 0.9859"
    );
    expect(httpMock).toHaveBeenCalledWith(
      "/api/timeseries?robot_id=example-manipulator-01&topic=joint_state&bucket=minute"
    );
  });

  it("keeps the scalar response contract and exposes a one-bucket point", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path === "/api/robots") return Promise.resolve(["example-wheeled-01"]);
      if (path.startsWith("/api/v1/message-types")) return Promise.resolve(["twist"]);
      if (path.startsWith("/api/timeseries?")) {
        return Promise.resolve({
          points: [{ t: "2026-01-01T00:00:00", x: 0.16, y: 0, z: 0, n: 160 }],
          columns: ["x", "y", "z"],
          all_null_columns: [],
        });
      }
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Explore />, {
      route: "/explore?robot=example-wheeled-01&topic=twist",
    });

    const chart = await screen.findByRole("button", { name: "Time series chart" });
    const summary = JSON.parse(chart.getAttribute("data-chart-summary") || "{}");
    expect(summary.legend).toEqual(["x", "y", "z"]);
    expect(summary.series[0]).toEqual({
      name: "x",
      showSymbol: true,
      data: [["2026-01-01T00:00:00", 0.16]],
    });
  });
});
