/*
 * The OBS nav link is gated on
 * `useObsHealthy()` (a poll of `GET /api/obs/health`), hidden by default --
 * the opposite default from `useIsProEdition` -- because the `obs` profile
 * is normally down and a dead nav link is the costlier mistake. The page
 * itself stays reachable by URL either way (see Home.test.tsx's assertion
 * that `/ui/obs` is never linked from the KPI degrade banner unless
 * healthy); this file pins the nav link's own on/off behaviour.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

const layoutAuth = vi.hoisted(() => ({
  snapshot: { ready: true, mode: "none", signedIn: false, error: null },
}));

vi.mock("../lib/api", () => ({ http: vi.fn() }));
vi.mock("../lib/auth", () => ({
  beginSignIn: vi.fn(),
  getAuthSnapshot: () => layoutAuth.snapshot,
  initializeAuth: vi.fn().mockResolvedValue(undefined),
  signOut: vi.fn(),
  subscribeAuth: () => () => {},
}));

import Layout from "../components/Layout";
import { renderWithProviders } from "./testUtils";
import { http } from "../lib/api";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

function mockRoutes(routes: Record<string, unknown>) {
  const allRoutes = {
    "/api/tenant": { scope: "test-scope" },
    "/api/v1/metrics/overview": {
      kpis: {
        uploads_last_hour: null,
        storage_bytes_total: null,
        episodes_awaiting_qa: null,
        exports_completed_24h: null,
      },
    },
    ...routes,
  };
  httpMock.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(allRoutes)) {
      if (path.startsWith(prefix)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    throw new Error(`unmocked path in test: ${path}`);
  });
}

describe("Layout OBS nav link", () => {
  it("offers the global theme action when Layout is rendered directly", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "pro" },
      "/api/obs/health": { status: { prometheus: { ok: false } } },
    });

    renderWithProviders(<Layout />);

    fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));

    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
    expect(localStorage.getItem("robot-vault.theme")).toBe("light");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("is absent while /api/obs/health is loading or errors (hidden by default)", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "pro" },
      "/api/obs/health": new Error("connection refused"),
    });

    renderWithProviders(<Layout />);

    await waitFor(() => expect(httpMock).toHaveBeenCalledWith("/api/obs/health"));
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Observability" })).not.toBeInTheDocument();
    });
  });

  it("is present once /api/obs/health reports prometheus healthy", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "pro" },
      "/api/obs/health": { status: { prometheus: { ok: true } } },
    });

    renderWithProviders(<Layout />);

    expect(await screen.findByRole("link", { name: "Observability" })).toHaveAttribute("href", "/obs");
  });

  it("stays absent when prometheus itself reports unhealthy", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "pro" },
      "/api/obs/health": { status: { prometheus: { ok: false } } },
    });

    renderWithProviders(<Layout />);

    await waitFor(() => expect(httpMock).toHaveBeenCalledWith("/api/obs/health"));
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Observability" })).not.toBeInTheDocument();
    });
  });

  it("keeps onboarding opt-in and skips curation tour steps in the core edition", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "core" },
      "/api/obs/health": { status: { prometheus: { ok: false } } },
    });

    renderWithProviders(<Layout />);

    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Review" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Verification" })).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("dialog", { name: "Start with the fleet overview" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open help" }));
    expect(screen.queryByText("Cut and verify a dataset")).not.toBeInTheDocument();
    expect(screen.getByText("The local guide covers intake and stream exploration.")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Start tour" }));
    expect(await screen.findByRole("dialog", { name: "Start with the fleet overview" })).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("heading", { name: "Review intake" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("heading", { name: "Reopen help any time" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Assess review findings" })).not.toBeInTheDocument();
  });

  it("provides a skip target, names icon utilities, and opens the bundled guide route", async () => {
    mockRoutes({
      "/api/v1/edition": { edition: "pro" },
      "/api/obs/health": { status: { prometheus: { ok: true } } },
    });

    renderWithProviders(<Layout />);

    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main-content");
    expect(document.querySelector("main#main-content")).toHaveAttribute("tabindex", "-1");
    expect(await screen.findByRole("link", { name: "Administration" })).toHaveAttribute("href", "/admin");
    expect(await screen.findByRole("link", { name: "Observability" })).toHaveAttribute("href", "/obs");

    fireEvent.click(screen.getByRole("button", { name: "Open help" }));
    expect(await screen.findByRole("link", { name: "Open guide" })).toHaveAttribute("href", "/help");
  });
});
