import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("echarts-for-react/lib/core", async () => {
  const React = await import("react");
  class MockECharts extends React.Component<any> {
    render() { return <div data-testid="echarts-canvas" />; }
  }
  return { default: MockECharts };
});

import {
  buildChartDataTable,
  default as Chart,
  takeBalancedChartRows,
} from "../components/Chart";

describe("Chart module interop", () => {
  it("builds its accessible rows from the exact plotted series", () => {
    expect(buildChartDataTable({
      series: [
        { name: "left joint", data: [["2026-01-01T00:00:00Z", 0.5]] },
        { name: "right joint", data: [["2026-01-01T00:00:00Z", -1.2]] },
      ],
    })).toEqual({
      seriesCount: 2,
      pointCount: 2,
      rows: [
        { series: "left joint", domain: "2026-01-01T00:00:00Z", value: "0.5" },
        { series: "right joint", domain: "2026-01-01T00:00:00Z", value: "-1.2" },
      ],
    });
  });

  it("uses visible x-axis categories for scalar series data", () => {
    expect(buildChartDataTable({
      xAxis: { type: "category", data: ["08:00", "09:00"] },
      series: [{ name: "uploads", data: [3, 5] }],
    }).rows).toEqual([
      { series: "uploads", domain: "08:00", value: "3" },
      { series: "uploads", domain: "09:00", value: "5" },
    ]);
  });

  it("formats numeric time-axis domains as exact ISO instants", () => {
    const epoch = Date.parse("2026-08-30T12:34:56.789Z");
    expect(buildChartDataTable({
      xAxis: { type: "time" },
      series: [{ name: "rate", data: [[epoch, 4.25]] }],
    }).rows).toEqual([
      { series: "rate", domain: "2026-08-30T12:34:56.789Z", value: "4.25" },
    ]);
  });

  it("balances capped table rows across series and samples their full ranges", () => {
    const rows = [
      ...Array.from({ length: 300 }, (_, index) => ({ series: "first", domain: String(index), value: String(index) })),
      ...Array.from({ length: 3 }, (_, index) => ({ series: "later", domain: String(index), value: String(index) })),
    ];
    const shown = takeBalancedChartRows(rows, 200);

    expect(shown).toHaveLength(200);
    expect(shown.filter((row) => row.series === "later")).toHaveLength(3);
    expect(shown.some((row) => row.series === "first" && row.domain === "299")).toBe(true);
  });

  it("exposes a concise summary, keyboard disclosure table, and selection announcement", () => {
    render(
      <Chart
        option={{ series: [{ name: "velocity", data: [["t0", 2.4]] }] }}
        accessibility={{ title: "Velocity history", summary: "One velocity series with one point." }}
        selectionAnnouncement="Selected velocity at t0: 2.4"
      />
    );

    const chart = screen.getByRole("group", { name: "Velocity history" });
    expect(within(chart).getByText("One velocity series with one point. 1 series, 1 point.")).toBeInTheDocument();
    expect(within(chart).getByText("View chart data (1 point)", { selector: "summary" })).toBeInTheDocument();
    const table = within(chart).getByRole("table", { name: "Velocity history data" });
    expect(within(table).getByRole("rowheader", { name: "velocity" })).toBeInTheDocument();
    expect(within(chart).getByRole("status")).toHaveTextContent("Selected velocity at t0: 2.4");
  });

  it("uses generic truncation guidance when no complete-data action is available", () => {
    render(
      <Chart
        option={{ series: [{ name: "signal", data: Array.from({ length: 205 }, (_, index) => [index, index]) }] }}
        accessibility={{ title: "Signal history" }}
      />,
    );

    expect(screen.getByText("Showing a balanced sample of 200 of 205 points.")).toBeInTheDocument();
    expect(screen.queryByText(/Export CSV/)).not.toBeInTheDocument();
  });

  it("spells out missing table values for assistive technology", () => {
    expect(buildChartDataTable({ series: [{ name: "signal", data: [["t0", null], ["t1", Number.NaN]] }] }).rows)
      .toEqual([
        { series: "signal", domain: "t0", value: "Not available" },
        { series: "signal", domain: "t1", value: "Not available" },
      ]);
  });
});
