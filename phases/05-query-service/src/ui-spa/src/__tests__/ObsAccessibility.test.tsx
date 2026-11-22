import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../lib/api", () => ({ http: vi.fn() }));
vi.mock("../components/Chart", () => ({
  default: ({ accessibility }: { accessibility?: { title?: string; summary?: string; tableLabel?: string } }) => (
    <div
      role="group"
      aria-label={accessibility?.title}
      data-summary={accessibility?.summary}
      data-table-label={accessibility?.tableLabel}
    />
  ),
}));

import { http } from "../lib/api";
import Obs from "../pages/Obs";
import { renderWithProviders } from "./testUtils";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

describe("Observability chart accessibility", () => {
  // A chart mounts only once its query returns at least one sample; an empty
  // window shows a text line instead of bare axes. One sample per series.
  it("gives every chart a task-specific name, summary, and table label", async () => {
    httpMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/obs/promql/range?")) {
        return Promise.resolve({ data: { result: [{ metric: {}, values: [[1_700_000_000, "1"]] }] } });
      }
      if (path === "/api/obs/alerts") return Promise.resolve({ data: { alerts: [] } });
      throw new Error(`unmocked path in test: ${path}`);
    });

    renderWithProviders(<Obs />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Run" }));

    const charts = [
      await screen.findByRole("group", { name: "Uploads completed over time" }),
      await screen.findByRole("group", { name: "Requests per second by path" }),
      await screen.findByRole("group", { name: "Average query duration over time" }),
      await screen.findByRole("group", { name: "Ad-hoc PromQL result" }),
    ];
    charts.forEach((chart) => {
      expect(chart.dataset.summary).toBeTruthy();
      expect(chart.dataset.tableLabel).toBeTruthy();
    });
    expect(screen.queryByRole("group", { name: "Data chart" })).not.toBeInTheDocument();
  });
});
