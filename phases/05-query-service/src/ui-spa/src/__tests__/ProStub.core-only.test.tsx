import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({ http: vi.fn() }));

import { http } from "../lib/api";
import HelpPage from "../pages/Help";
import Home from "../pages/Home";
import { ProRoute } from "../components/ProRoute";
import { PRO_UPGRADE_URL } from "../components/AvailableInPro";
import { SOURCE_REPO_URL, SourceNotice } from "../components/Layout";
import { pro } from "../pro";
import { renderWithProviders } from "./testUtils";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  httpMock.mockResolvedValue({ edition: "pro" });
});

function mockHomeRoutes(routes: Record<string, unknown>) {
  httpMock.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(routes)) {
      if (path.startsWith(prefix)) return Promise.resolve(value);
    }
    throw new Error(`unmocked path in test: ${path}`);
  });
}

const BASE_ROUTES = {
  "/api/v1/edition": { edition: "pro" },
  "/api/tenant": { scope: "test-scope" },
  "/api/v1/robots/list": { items: [] },
  "/api/obs/health": { status: { prometheus: { ok: false } } },
};

// Runs on the core tree only: the Pro build excludes this test, because there
// `../pro` is the real Pro module.
describe("core build without the Pro UI", () => {
  it("has the stub Pro module", () => {
    expect(pro).toEqual({ installed: false, routes: {}, HomeAttention: null });
  });

  it.each([
    ["episodes", "Episodes"],
    ["datasets", "Datasets"],
  ] as const)("renders the panel at the %s route, even against a Pro backend", async (route, feature) => {
    renderWithProviders(<ProRoute route={route} feature={feature} />);
    expect(await screen.findByRole("heading", { name: `${feature} is available in Pro` })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Learn about Ingotron Pro" })).toHaveAttribute("href", PRO_UPGRADE_URL);
  });

  it("links the footer to the release tag with the licence", () => {
    renderWithProviders(<SourceNotice version="0.10.0" />);
    expect(screen.getByText("v0.10.0")).toBeInTheDocument();
    expect(screen.getByText("AGPL-3.0")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source" })).toHaveAttribute("href", `${SOURCE_REPO_URL}/tree/v0.10.0`);
  });

  it("shows the Help panels against a Pro backend too", async () => {
    renderWithProviders(<HelpPage />, { route: "/help" });
    await screen.findByRole("link", { name: "Open Intake" });
    expect(await screen.findAllByTestId("available-in-pro")).toHaveLength(2);
  });

  it("shows Available in Pro on the Review and Verification tiles and never asks for datasets", async () => {
    mockHomeRoutes({
      ...BASE_ROUTES,
      "/api/v1/metrics/overview": {
        kpis: { uploads_last_hour: 0, storage_bytes_total: 0, episodes_awaiting_qa: 0, exports_completed_24h: 0 },
      },
    });

    renderWithProviders(<Home />);

    expect(await screen.findAllByText("Available in Pro")).toHaveLength(2);
    expect(httpMock.mock.calls.map(([path]) => path).some((p: string) => p.startsWith("/api/v1/datasets"))).toBe(false);
  });
});
