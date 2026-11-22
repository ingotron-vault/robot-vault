import { forwardRef, type ComponentProps, useId, useMemo } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  LegendScrollComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

// Importing `echarts-for-react`'s default entry pulls every chart, coordinate
// system and renderer into the browser bundle. Robot Vault currently uses only
// line/bar Cartesian charts and the components below, so register that exact
// surface and keep the lazy Explore/Observability chunk small.
echarts.use([
  BarChart,
  LineChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  LegendScrollComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export interface ChartAccessibility {
  /** Names the chart region and its data table. */
  title: string;
  /** Describes what the chart contains without trying to narrate a trend. */
  summary?: string;
  tableLabel?: string;
  /** Optional guidance shown only when the keyboard table is capped. */
  truncatedMessage?: string;
}

export interface ChartDataRow {
  series: string;
  domain: string;
  value: string;
}

export interface ChartDataTable {
  rows: ChartDataRow[];
  pointCount: number;
  seriesCount: number;
}

type ChartProps = Omit<ComponentProps<typeof ReactEChartsCore>, "echarts"> & {
  accessibility?: ChartAccessibility;
  /** Polite live text set by the parent after a canvas point is selected. */
  selectionAnnouncement?: string;
};
type EChartsCoreComponent = typeof ReactEChartsCore;
type EChartsCoreModule =
  | EChartsCoreComponent
  | { default: EChartsCoreComponent };

// Vite 8 preserves the CommonJS exports object for this deep import, while
// TypeScript sees the package declaration's default class. Unwrap that object
// once so React always receives the component class rather than a module.
export function resolveEChartsCore(
  moduleValue: EChartsCoreModule,
): EChartsCoreComponent {
  if (
    typeof moduleValue === "object" &&
    moduleValue !== null &&
    "default" in moduleValue
  ) {
    return moduleValue.default;
  }
  return moduleValue;
}

export const EChartsCoreComponent = resolveEChartsCore(
  ReactEChartsCore as unknown as EChartsCoreModule,
);

function printable(value: unknown): string {
  if (value == null) return "Not available";
  if (typeof value === "number" && !Number.isFinite(value)) return "Not available";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function printableDomain(value: unknown, timeAxis: boolean): string {
  if (timeAxis && typeof value === "number" && Number.isFinite(value)) {
    const instant = new Date(value);
    if (Number.isFinite(instant.getTime())) return instant.toISOString();
  }
  return printable(value);
}

/** Builds the accessible table from the same series data passed to ECharts. */
export function buildChartDataTable(option: unknown): ChartDataTable {
  if (!option || typeof option !== "object") {
    return { rows: [], pointCount: 0, seriesCount: 0 };
  }
  const rawSeries = (option as { series?: unknown }).series;
  const seriesList = Array.isArray(rawSeries) ? rawSeries : rawSeries ? [rawSeries] : [];
  const rawXAxis = (option as { xAxis?: unknown }).xAxis;
  const xAxis = Array.isArray(rawXAxis) ? rawXAxis[0] : rawXAxis;
  const categories = xAxis && typeof xAxis === "object" && Array.isArray((xAxis as { data?: unknown }).data)
    ? (xAxis as { data: unknown[] }).data
    : [];
  const timeAxis = !!xAxis && typeof xAxis === "object" && (xAxis as { type?: unknown }).type === "time";
  const rows: ChartDataRow[] = [];

  seriesList.forEach((raw, seriesIndex) => {
    if (!raw || typeof raw !== "object") return;
    const series = raw as { name?: unknown; data?: unknown };
    const name = typeof series.name === "string" && series.name ? series.name : `Series ${seriesIndex + 1}`;
    const data = Array.isArray(series.data) ? series.data : [];
    data.forEach((item, pointIndex) => {
      let domain: unknown = categories[pointIndex] ?? pointIndex + 1;
      let value: unknown = item;
      if (Array.isArray(item)) {
        domain = item[0] ?? pointIndex + 1;
        value = item[1];
      } else if (item && typeof item === "object") {
        const datum = item as { name?: unknown; value?: unknown };
        domain = datum.name ?? categories[pointIndex] ?? pointIndex + 1;
        if (Array.isArray(datum.value)) {
          domain = datum.value[0] ?? domain;
          value = datum.value[1];
        } else {
          value = datum.value;
        }
      }
      rows.push({ series: name, domain: printableDomain(domain, timeAxis), value: printable(value) });
    });
  });

  return { rows, pointCount: rows.length, seriesCount: seriesList.length };
}

function evenlySample<T>(values: T[], count: number): T[] {
  if (count >= values.length) return values;
  if (count <= 0) return [];
  if (count === 1) return [values[0]];
  return Array.from({ length: count }, (_, index) => (
    values[Math.round(index * (values.length - 1) / (count - 1))]
  ));
}

/** Caps a large table without allowing the first series to consume every row. */
export function takeBalancedChartRows(rows: ChartDataRow[], limit = 200): ChartDataRow[] {
  if (rows.length <= limit) return rows;
  if (limit <= 0) return [];

  const buckets = new Map<string, ChartDataRow[]>();
  rows.forEach((row) => {
    const bucket = buckets.get(row.series);
    if (bucket) bucket.push(row);
    else buckets.set(row.series, [row]);
  });
  const groups = [...buckets.values()];
  if (groups.length > limit) {
    return evenlySample(groups, limit).map((group) => group[0]);
  }

  const quotas = groups.map(() => Math.floor(limit / groups.length));
  let allocated = quotas.reduce((total, quota, index) => total + Math.min(quota, groups[index].length), 0);
  quotas.forEach((quota, index) => { quotas[index] = Math.min(quota, groups[index].length); });
  while (allocated < limit) {
    let added = false;
    groups.forEach((group, index) => {
      if (allocated >= limit || quotas[index] >= group.length) return;
      quotas[index] += 1;
      allocated += 1;
      added = true;
    });
    if (!added) break;
  }

  return groups.flatMap((group, index) => evenlySample(group, quotas[index]));
}

const Chart = forwardRef<ReactEChartsCore, ChartProps>(function Chart(props, ref) {
  const {
    accessibility,
    selectionAnnouncement,
    option,
    ...chartProps
  } = props;
  const summaryId = useId();
  const table = useMemo(() => buildChartDataTable(option), [option]);
  const title = accessibility?.title ?? "Data chart";
  const countSummary = `${table.seriesCount.toLocaleString()} ${table.seriesCount === 1 ? "series" : "series"}, ${table.pointCount.toLocaleString()} ${table.pointCount === 1 ? "point" : "points"}.`;
  const summary = accessibility?.summary ? `${accessibility.summary} ${countSummary}` : countSummary;
  const shownRows = useMemo(() => takeBalancedChartRows(table.rows, 200), [table.rows]);

  return (
    <div className="rv-chart" role="group" aria-label={title} aria-describedby={summaryId}>
      <p id={summaryId} className="rv-visually-hidden">{summary}</p>
      <div aria-hidden="true">
        <EChartsCoreComponent ref={ref} echarts={echarts} option={option} {...chartProps} />
      </div>
      {table.pointCount > 0 ? (
        <details className="rv-chart__data">
          <summary>View chart data ({table.pointCount.toLocaleString()} {table.pointCount === 1 ? "point" : "points"})</summary>
          <div className="rv-chart__table-wrap">
            <table>
              <caption>{accessibility?.tableLabel ?? `${title} data`}</caption>
              <thead><tr><th scope="col">Series</th><th scope="col">Time or category</th><th scope="col">Value</th></tr></thead>
              <tbody>
                {shownRows.map((row, index) => (
                  <tr key={`${row.series}-${row.domain}-${index}`}>
                    <th scope="row">{row.series}</th><td>{row.domain}</td><td>{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {shownRows.length < table.pointCount ? (
            <p className="rv-chart__limit">
              {accessibility?.truncatedMessage ?? `Showing a balanced sample of ${shownRows.length.toLocaleString()} of ${table.pointCount.toLocaleString()} points.`}
            </p>
          ) : null}
        </details>
      ) : null}
      <p className="rv-visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {selectionAnnouncement ?? ""}
      </p>
    </div>
  );
});

export default Chart;
