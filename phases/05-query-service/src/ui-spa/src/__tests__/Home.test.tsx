/*
 * Home's KPI tiles are Postgres-backed
 * (`uploads_last_hour`/`storage_bytes_total`/...), not Prometheus-backed,
 * and are meant to be
 * populated in every deployment rather than degrading whenever the opt-in
 * `obs` profile isn't running. This pins the new field names actually reach
 * the tiles, `storage_bytes_total` goes through the page's byte formatter
 * (not a raw number), and the degrade banner reads the new Postgres-only
 * copy -- not the old Prometheus wording.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", () => ({ http: vi.fn() }));
vi.mock("../lib/auth", () => ({ authorizedFetch: vi.fn() }));

import Home from "../pages/Home";
import { renderWithProviders } from "./testUtils";
import { http } from "../lib/api";
import { authorizedFetch } from "../lib/auth";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;
const authorizedFetchMock = authorizedFetch as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  vi.clearAllMocks();
});

function mockRoutes(routes: Record<string, unknown>) {
  httpMock.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(routes)) {
      if (path.startsWith(prefix)) {
        if (typeof value === "function") return Promise.resolve((value as () => unknown)());
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    throw new Error(`unmocked path in test: ${path}`);
  });
}

const BASE_ROUTES = {
  "/api/v1/edition": { edition: "pro" },
  "/api/tenant": { scope: "test-scope" },
  "/api/v1/robots/list": { items: [] },
  "/api/v1/datasets": {
    items: [],
    page: { total_count: 0 },
  },
  "/api/obs/health": { status: { prometheus: { ok: false } } },
};

describe("Home KPI tiles", () => {
  it("sends a first-time operator directly to Add robot", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/v1/metrics/overview": {
        kpis: { uploads_last_hour: 0, storage_bytes_total: 0, episodes_awaiting_qa: 0, exports_completed_24h: 0 },
      },
    });

    renderWithProviders(<Home />);

    expect(await screen.findByRole("link", { name: "Add your first robot" })).toHaveAttribute(
      "href",
      "/robots?add=1"
    );
  });

  it("renders the four Postgres-backed KPIs, bytes formatted", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/v1/metrics/overview": {
        kpis: {
          uploads_last_hour: 3,
          storage_bytes_total: 2097152, // 2.0 MB
          episodes_awaiting_qa: 4,
          exports_completed_24h: 7,
        },
      },
    });

    renderWithProviders(<Home />);

    // "Uploads · 1h" is not asserted by label alone -- the header's quick-link
    // button carries the identical text, so the tile is identified by its
    // hint instead, which is unique on the page.
    expect(await screen.findByText("completed or in flight, last 60 minutes")).toBeInTheDocument();
    expect(screen.getByText("Stored")).toBeInTheDocument();
    expect(screen.getByText("Awaiting QA")).toBeInTheDocument();
    expect(screen.getByText("Exports · 24h")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("3")).toBeInTheDocument();
      expect(screen.getByText("2.0 MB")).toBeInTheDocument();
      expect(screen.getByText("4")).toBeInTheDocument();
      expect(screen.getByText("7")).toBeInTheDocument();
    });

    // Nothing on the page names Prometheus/Grafana any more.
    expect(screen.queryByText(/prometheus/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/grafana/i)).not.toBeInTheDocument();
  });

  it("renders a null KPI as a dash instead of fabricating a number", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/v1/metrics/overview": {
        kpis: {
          uploads_last_hour: 1,
          storage_bytes_total: 0,
          episodes_awaiting_qa: null,
          exports_completed_24h: 0,
        },
      },
    });

    renderWithProviders(<Home />);

    await screen.findByText("Awaiting QA");
    await waitFor(() => {
      expect(screen.getByTitle("No data yet for this metric")).toBeInTheDocument();
    });
  });

  it("shows the DB-failure banner, without naming Prometheus, when the endpoint errors", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/v1/metrics/overview": new Error("boom"),
    });

    renderWithProviders(<Home />);

    // `metricsQ` sets `retry: 1` itself (Home.tsx), which overrides
    // testUtils' `retry: false` default and costs one real react-query
    // backoff delay before the banner appears -- past the default 1000ms
    // findBy timeout.
    expect(
      await screen.findByText(
        "Stats unavailable — the query service could not reach the database. The rest of this page is unaffected.",
        {},
        { timeout: 3000 }
      )
    ).toBeInTheDocument();
  });

  it("only links to OBS from the degrade banner when the obs profile is healthy", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/obs/health": { status: { prometheus: { ok: true } } },
      "/api/v1/metrics/overview": new Error("boom"),
    });

    renderWithProviders(<Home />);

    await screen.findByText(/Stats unavailable/, {}, { timeout: 3000 });
    await waitFor(() => {
      expect(screen.getByRole("link", { name: "Open OBS" })).toBeInTheDocument();
    });
  });

  it("does not report a quiet attention queue when the fleet summary failed", async () => {
    mockRoutes({
      ...BASE_ROUTES,
      "/api/v1/robots/list": new Error("registry unavailable"),
      "/api/v1/metrics/overview": {
        kpis: {
          uploads_last_hour: 1,
          storage_bytes_total: 1024,
          episodes_awaiting_qa: 0,
          exports_completed_24h: 1,
        },
      },
    });

    renderWithProviders(<Home />);

    expect(
      await screen.findByText(/Fleet summary unavailable; upload failures and stale robots cannot be assessed right now/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/No failed or in-flight uploads are reported/)).not.toBeInTheDocument();
  });
});
