import { describe, expect, it } from "vitest";
import { buildDurationOption, getTelemetryState } from "../pages/Obs";
import type { OrbitChartTheme } from "../lib/chartTheme";
import { HttpError } from "../lib/api";

const lightChartTheme: OrbitChartTheme = {
  series: ["#0f766e"],
  text: "#52525b",
  axis: "#a1a1aa",
  grid: "#e4e4e7",
  slider: "#f4f4f5",
  selection: "#ccfbf1",
  tooltipBackground: "#18181b",
  tooltipBorder: "#3f3f46",
  tooltipText: "#fafafa",
  warning: "#a16207",
  danger: "#b91c1c",
};

describe("observability duration thresholds", () => {
  it("attaches threshold markers to a line series", () => {
    const option = buildDurationOption([
      {
        metric: { job: "query" },
        values: [[1, "0.25"], [2, "0.75"]],
      },
    ]);

    expect(option).not.toHaveProperty("markLine");
    expect(option.series[0].markLine.data).toEqual([
      expect.objectContaining({ yAxis: 0.5 }),
      expect.objectContaining({ yAxis: 1.0 }),
    ]);
  });

  it("does not invent an empty series solely to draw thresholds", () => {
    expect(buildDurationOption([]).series).toEqual([]);
  });

  it("uses semantic theme colours for series, axes, and thresholds", () => {
    const option = buildDurationOption([
      {
        metric: { job: "query" },
        values: [[1, "0.25"], [2, "0.75"]],
      },
    ], lightChartTheme);

    expect(option.textStyle.color).toBe(lightChartTheme.text);
    expect(option.xAxis.axisLine.lineStyle.color).toBe(lightChartTheme.axis);
    expect(option.series[0].color).toBe(lightChartTheme.series[0]);
    expect(option.series[0].markLine.lineStyle.color).toBe(lightChartTheme.warning);
    expect(option.series[0].markLine.data[1].lineStyle.color).toBe(lightChartTheme.danger);
  });
});

describe("observability connection state", () => {
  it("does not claim Prometheus is connected while every query is failing", () => {
    expect(getTelemetryState([
      { isSuccess: false, isError: true },
      { isSuccess: false, isError: true },
      { isSuccess: false, isError: true },
    ])).toEqual({ tone: "unavailable", label: "Prometheus unavailable" });
  });

  it("distinguishes checking, degraded, and fully connected telemetry", () => {
    expect(getTelemetryState([
      { isSuccess: false, isError: false },
      { isSuccess: false, isError: false },
    ])).toEqual({ tone: "checking", label: "Checking Prometheus" });
    expect(getTelemetryState([
      { isSuccess: true, isError: false },
      { isSuccess: false, isError: true },
    ])).toEqual({ tone: "degraded", label: "Telemetry degraded" });
    expect(getTelemetryState([
      { isSuccess: true, isError: false },
      { isSuccess: true, isError: false },
    ])).toEqual({ tone: "connected", label: "Prometheus connected" });
  });

  it("tells a viewer that PromQL needs the operator role instead of reporting an outage", () => {
    const forbidden = new HttpError(403, "Forbidden");
    expect(getTelemetryState([
      { isSuccess: false, isError: true, error: forbidden },
      { isSuccess: false, isError: true, error: forbidden },
    ])).toEqual({ tone: "unavailable", label: "Operator role required" });
  });

  it("still reports an outage when a failure is not a role refusal", () => {
    expect(getTelemetryState([
      { isSuccess: false, isError: true, error: new HttpError(403, "Forbidden") },
      { isSuccess: false, isError: true, error: new HttpError(502, "prometheus_unavailable") },
    ])).toEqual({ tone: "unavailable", label: "Prometheus unavailable" });
  });
});
