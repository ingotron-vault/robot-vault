import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Chart from "../../components/Chart";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import { useOrbitTheme } from "../../components/OrbitTheme";
import { http, HttpError } from "../../lib/api";
import {
  type OrbitChartTheme,
  orbitChartAxis,
  orbitChartTooltip,
  readOrbitChartTheme,
} from "../../lib/chartTheme";
import { downloadCsv } from "../../lib/csv";
import { Download, Play, RefreshCw } from "lucide-react";
import "./obs.css";

const LINE_PATTERNS = ["solid", "dashed", "dotted"];

function parseRange(v: string) {
  const now = Math.floor(Date.now() / 1000);
  const n = parseInt(v);
  if (v.endsWith("h")) return { start: now - n * 3600, end: now };
  if (v.endsWith("d")) return { start: now - n * 86400, end: now };
  return { start: now - 3600, end: now };
}

function downsample(values: any[], maxPoints = 600) {
  if (!Array.isArray(values) || values.length <= maxPoints) return values;
  const bucket = Math.ceil(values.length / maxPoints);
  const out: any[] = [];
  for (let i = 0; i < values.length; i += bucket) {
    out.push(values[i]);
  }
  return out;
}

function toDatasets(
  result: any[],
  labelKeys?: string[],
  topN?: number,
  chartTheme: OrbitChartTheme = readOrbitChartTheme(),
): Array<Record<string, any>> {
  let series = (result || []).map((s: any) => {
    const m = s.metric || {}; let lbl = "";
    for (const k of (labelKeys || [])) { if (m[k]) { lbl = m[k]; break; } }
    if (!lbl) lbl = Object.keys(m).length ? JSON.stringify(m) : "series";
    const pts = downsample((s.values || []).map((v: any[]) => [v[0] * 1000, Number(v[1])]), 800);
    const sum = pts.reduce((acc: number, p: any[]) => acc + (Number(p?.[1]) || 0), 0);
    return { name: lbl, sum, data: pts };
  });
  if (typeof topN === "number" && Number.isFinite(topN) && topN > 0) {
    series.sort((a, b) => b.sum - a.sum);
    series = series.slice(0, topN);
  }
  return series.map((s, idx) => ({
    name: s.name,
    type: "line",
    showSymbol: false,
    data: s.data,
    lineStyle: { width: 2, type: LINE_PATTERNS[idx % LINE_PATTERNS.length] },
    color: chartTheme.series[idx % chartTheme.series.length],
  }));
}

export function buildDurationOption(
  result: any[],
  chartTheme: OrbitChartTheme = readOrbitChartTheme(),
) {
  const series = toDatasets(result || [], ["type", "job"], undefined, chartTheme);
  // markLine is a series option, not a top-level ECharts option. Putting it
  // at the root is silently ignored, which hid both latency thresholds.
  if (series.length > 0) {
    series[0] = {
      ...series[0],
      markLine: {
        symbol: "none",
        lineStyle: { type: "dashed", color: chartTheme.warning },
        data: [
          { yAxis: 0.5, label: { formatter: "0.5s" } },
          { yAxis: 1.0, lineStyle: { color: chartTheme.danger }, label: { formatter: "1.0s" } },
        ],
      },
    };
  }
  const axis = orbitChartAxis(chartTheme);
  return {
    backgroundColor: "transparent",
    textStyle: { color: chartTheme.text },
    tooltip: orbitChartTooltip(chartTheme),
    xAxis: { type: "time", ...axis }, yAxis: { type: "value", ...axis },
    series,
    grid: { left: 50, right: 20, top: 30, bottom: 40 },
  };
}

function isRoleRefusal(error: unknown): boolean {
  return error instanceof HttpError && error.status === 403;
}

export function getTelemetryState(
  queries: Array<{ isSuccess: boolean; isError: boolean; error?: unknown }>,
) {
  const successes = queries.filter((query) => query.isSuccess).length;
  const errors = queries.filter((query) => query.isError).length;
  if (queries.length === 0) return { tone: "checking", label: "Checking Prometheus" };
  if (successes === queries.length) return { tone: "connected", label: "Prometheus connected" };
  // PromQL is operator-only on the server. A viewer gets 403 on every panel;
  // that is a permission limit, not a Prometheus outage.
  if (errors === queries.length && queries.every((query) => isRoleRefusal(query.error))) {
    return { tone: "unavailable", label: "Operator role required" };
  }
  if (errors === queries.length) return { tone: "unavailable", label: "Prometheus unavailable" };
  if (successes > 0 || errors > 0) return { tone: "degraded", label: "Telemetry degraded" };
  return { tone: "checking", label: "Checking Prometheus" };
}

export default function Obs() {
  const { theme } = useOrbitTheme();
  const chartTheme = useMemo(() => readOrbitChartTheme(), [theme]);
  const [range, setRange] = useState<string>("6h");
  const [step, setStep] = useState<string>("1m");
  const [auto, setAuto] = useState<boolean>(false);
  const [fPath, setFPath] = useState<string>("");
  const [fRobot, setFRobot] = useState<string>("");
  const [topN, setTopN] = useState<number>(8);

  const { start, end } = useMemo(() => parseRange(range), [range]);

  const prom = async (query: string) => {
    const p = new URLSearchParams({ query, start: String(start), end: String(end), step });
    return http<any>(`/api/obs/promql/range?${p.toString()}`);
  };

  const retryUnlessForbidden = (failureCount: number, error: unknown) =>
    !isRoleRefusal(error) && failureCount < 3;

  const matcher = (filters: Record<string, string>) => {
    const parts: string[] = [];
    Object.entries(filters).forEach(([k, v]) => {
      const vv = (v || "").trim(); if (!vv) return;
      const esc = vv.replace(/"/g, '\\"');
      parts.push(`${k}=~"${esc}"`);
    });
    return parts.length ? `{${parts.join(',')}}` : '';
  };

  const uploadsQ = useQuery({
    queryKey: ["obs","uploads", range, step, fRobot],
    retry: retryUnlessForbidden,
    queryFn: () => prom(`rate(uploads_completed${matcher({ robot: fRobot, robot_id: fRobot })}[5m])`),
  });
  const rpsQ = useQuery({
    queryKey: ["obs","rps", range, step, fPath],
    retry: retryUnlessForbidden,
    queryFn: () => prom(`sum by (path) (rate(query_service_requests_total${matcher({ path: fPath })}[5m]))`),
  });
  const qdurQ = useQuery({
    queryKey: ["obs","qdur", range, step],
    retry: retryUnlessForbidden,
    queryFn: () => prom(`sum(rate(query_engine_duration_seconds_sum[5m])) / sum(rate(query_engine_duration_seconds_count[5m]))`),
  });

  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => { uploadsQ.refetch(); rpsQ.refetch(); qdurQ.refetch(); }, 15_000);
    return () => clearInterval(t);
  }, [auto]);

  const uploadsOpt = useMemo(() => {
    const axis = orbitChartAxis(chartTheme);
    return {
      backgroundColor: "transparent", textStyle: { color: chartTheme.text }, tooltip: orbitChartTooltip(chartTheme),
      xAxis: { type: "time", ...axis }, yAxis: { type: "value", ...axis },
      series: toDatasets((uploadsQ.data?.data?.result) || [], [], undefined, chartTheme),
      grid: { left: 50, right: 20, top: 30, bottom: 40 },
    };
  }, [uploadsQ.data, chartTheme]);
  const rpsOpt = useMemo(() => {
    const axis = orbitChartAxis(chartTheme);
    return {
      backgroundColor: "transparent", textStyle: { color: chartTheme.text }, tooltip: orbitChartTooltip(chartTheme),
      xAxis: { type: "time", ...axis }, yAxis: { type: "value", ...axis },
      legend: { type: "scroll", bottom: 0, textStyle: { color: chartTheme.text } },
      series: toDatasets((rpsQ.data?.data?.result) || [], ["path","job"], topN, chartTheme),
      grid: { left: 50, right: 20, top: 30, bottom: 80 },
    };
  }, [rpsQ.data, topN, chartTheme]);
  const qdurOpt = useMemo(
    () => buildDurationOption(qdurQ.data?.data?.result || [], chartTheme),
    [qdurQ.data, chartTheme],
  );

  // Explore panel
  const [expr, setExpr] = useState<string>("sum(rate(query_service_requests_total[5m]))");
  const [explore, setExplore] = useState<any>(null);
  const [exploreError, setExploreError] = useState<string | null>(null);
  const exploreOpt = useMemo(() => {
    const axis = orbitChartAxis(chartTheme);
    return {
      backgroundColor: "transparent",
      textStyle: { color: chartTheme.text },
      tooltip: orbitChartTooltip(chartTheme),
      xAxis: { type: "time", ...axis },
      yAxis: { type: "value", ...axis },
      legend: { type: "scroll", bottom: 0, textStyle: { color: chartTheme.text } },
      grid: { left: 50, right: 20, top: 30, bottom: 60 },
      series: toDatasets((explore?.data?.result) || [], [], undefined, chartTheme),
    };
  }, [explore, chartTheme]);
  const runExplore = async () => {
    try {
      const res = await prom(expr);
      setExplore(res);
      setExploreError(null);
    } catch (error) {
      setExplore(null);
      setExploreError(
        isRoleRefusal(error)
          ? "PromQL queries need the operator or admin role."
          : "The query did not run. Check the expression and that Prometheus is running.",
      );
    }
  };
  const exportExploreCsv = () => {
    try {
      const series = explore?.data?.result || [];
      if (!series.length) return;
      const rows: Array<Array<string | number>> = [];
      let idx = 0;
      for (const s of series) {
        for (const v of (s.values || [])) rows.push([idx, new Date(v[0] * 1000).toISOString(), v[1]]);
        idx += 1;
      }
      downloadCsv("prom_explore.csv", ["series", "ts", "value"], rows);
    } catch {}
  };

  // Alerts
  const alertsQ = useQuery({
    queryKey: ["obs","alerts"],
    queryFn: () => http<any>(`/api/obs/alerts`),
    refetchInterval: 60_000,
  });

  const telemetryState = getTelemetryState([uploadsQ, rpsQ, qdurQ]);
  const windowLabel = RANGE_LABELS[range] ?? range;

  return (
    <div className="rv-obs">
      <PageHeader
        kicker="System / Observability"
        title="System telemetry"
        description="Inspect ingest throughput, query traffic, latency thresholds, and active Prometheus alerts."
        actions={<PageStatus tone={STATUS_TONE[telemetryState.tone] ?? "info"} role="status">{telemetryState.label}</PageStatus>}
      />

      <section id="obs-window" className="rv-obs-filter-bar" aria-labelledby="rv-obs-controls-title">
        <h2 id="rv-obs-controls-title" className="rv-visually-hidden">Telemetry window</h2>
        <label className="rv-obs-field is-range"><span>Range</span><select value={range} onChange={(e)=> setRange(e.target.value)}><option value="1h">Last 1h</option><option value="6h">Last 6h</option><option value="24h">Last 24h</option><option value="7d">Last 7d</option></select></label>
        <label className="rv-obs-field is-step"><span>Step</span><select value={step} onChange={(e)=> setStep(e.target.value)}><option>15s</option><option>1m</option><option>5m</option></select></label>
        <label className="rv-obs-field is-regex"><span>Path regex</span><input className="is-mono" placeholder="^/api/" value={fPath} onChange={(e)=> setFPath(e.target.value)} /></label>
        <label className="rv-obs-field is-regex"><span>Robot regex</span><input className="is-mono" placeholder="^demo$" value={fRobot} onChange={(e)=> setFRobot(e.target.value)} /></label>
        <label className="rv-obs-field is-top"><span>Top N</span><input type="number" min={1} max={20} value={topN} onChange={(e)=> setTopN(Number(e.target.value||8))} /></label>
        <div className="rv-obs-actions">
          <label className="rv-obs-auto"><input type="checkbox" checked={auto} onChange={(e)=> setAuto(e.target.checked)} /> Auto-refresh</label>
          <button className="rv-obs-button" onClick={()=>{ uploadsQ.refetch(); rpsQ.refetch(); qdurQ.refetch(); }}><RefreshCw size={14} aria-hidden="true" /> Refresh</button>
        </div>
      </section>

      <div className="rv-obs-chart-grid">
        <section id="obs-signals" className="rv-obs-panel rv-obs-chart rv-orbit-panel" aria-labelledby="rv-obs-uploads">
          <PanelHead id="rv-obs-uploads" kicker="Ingest" title="Uploads completed" note="per second · 5m rate" />
          <ChartArea query={uploadsQ} option={uploadsOpt} windowLabel={windowLabel}>
            <Chart option={uploadsOpt} style={{ height: 280 }} accessibility={{ title: "Uploads completed over time", summary: `Completed-upload rate series for the selected ${range} window.`, tableLabel: "Completed-upload rate data" }} />
          </ChartArea>
        </section>
        <section className="rv-obs-panel rv-obs-chart rv-orbit-panel" aria-labelledby="rv-obs-rps">
          <PanelHead id="rv-obs-rps" kicker="HTTP" title="Requests per second" note={`top ${topN} paths · 5m rate`} />
          <ChartArea query={rpsQ} option={rpsOpt} windowLabel={windowLabel}>
            <Chart option={rpsOpt} style={{ height: 320 }} accessibility={{ title: "Requests per second by path", summary: `Request-rate series for the selected ${range} window, limited to the top ${topN} paths.`, tableLabel: "Request-rate data by path" }} />
          </ChartArea>
        </section>
        <section className="rv-obs-panel rv-obs-chart rv-orbit-panel is-wide" aria-labelledby="rv-obs-duration">
          <PanelHead id="rv-obs-duration" kicker="Query engine" title={<>Average duration <span>seconds</span></>} note="thresholds 0.5s and 1.0s" />
          <ChartArea query={qdurQ} option={qdurOpt} windowLabel={windowLabel}>
            <Chart option={qdurOpt} style={{ height: 280 }} accessibility={{ title: "Average query duration over time", summary: `Average query-engine duration for the selected ${range} window, with thresholds at 0.5 and 1 second.`, tableLabel: "Average query-duration data" }} />
          </ChartArea>
        </section>
      </div>

      <div className="rv-obs-lower-grid">
        <section id="obs-query" className="rv-obs-panel rv-obs-prom rv-orbit-panel" aria-labelledby="rv-obs-prom-title">
          <PanelHead id="rv-obs-prom-title" kicker="PromQL" title="Ad-hoc explorer" note={`last ${windowLabel} · step ${step}`} />
          <div className="rv-obs-panel-body">
            <div className="rv-obs-prom-controls"><input aria-label="PromQL expression" value={expr} onChange={(e)=> setExpr(e.target.value)} /><button className="rv-obs-button is-primary" onClick={runExplore}><Play size={14} aria-hidden="true" /> Run</button><button className="rv-obs-button" onClick={exportExploreCsv}><Download size={14} aria-hidden="true" /> CSV</button></div>
            {exploreError ? <p className="rv-obs-prom-error" role="alert">{exploreError}</p> : null}
          </div>
          {explore && hasPoints(exploreOpt) ? (
            <div className="rv-obs-chart-body">
              <Chart option={exploreOpt} style={{ height: 280 }} accessibility={{ title: "Ad-hoc PromQL result", summary: `PromQL result series for the current expression and selected ${range} window.`, tableLabel: "Ad-hoc PromQL result data", truncatedMessage: "Showing a balanced sample of plotted points. Use CSV for the complete PromQL result." }} />
            </div>
          ) : exploreError ? null : (
            <p className="rv-obs-chart-empty">
              {explore ? `The expression returned no samples for the last ${windowLabel}.` : "Run an expression to plot its result for the selected window."}
            </p>
          )}
        </section>

        <section id="obs-alerts" className="rv-obs-panel rv-obs-alerts rv-orbit-panel" aria-labelledby="rv-obs-alert-title">
          <PanelHead id="rv-obs-alert-title" kicker="Alertmanager" title="Active alerts" note={alertsQ.data ? `${((alertsQ.data?.data?.alerts) || []).length} active` : undefined} />
          {(() => {
            if (alertsQ.isLoading) return <div className="rv-obs-empty"><span className="is-checking"><i /> Checking</span><p>Loading alert state…</p></div>;
            if (alertsQ.isError) return <div className="rv-obs-empty"><span className="is-unavailable"><i /> Unavailable</span><p>Alertmanager did not answer.</p></div>;
            const items = (alertsQ.data?.data?.alerts) || [];
            if (!items.length) return <div className="rv-obs-empty"><span><i /> Healthy</span><p>No active alerts.</p></div>;
            return <div className="rv-obs-alert-list">{items.map((a: any, i: number) => <article key={i}><span className={`is-${String(a?.labels?.severity || "info").toLowerCase()}`}>{String(a?.labels?.severity || "info")}</span><div><b>{String(a?.labels?.alertname || "alert")}</b><p>{String(a?.annotations?.summary || a?.annotations?.description || "No description")}</p></div></article>)}</div>;
          })()}
        </section>
      </div>
    </div>
  );
}

const RANGE_LABELS: Record<string, string> = { "1h": "1 hour", "6h": "6 hours", "24h": "24 hours", "7d": "7 days" };

const STATUS_TONE: Record<string, "info" | "ok" | "warn" | "error"> = {
  checking: "info",
  connected: "ok",
  degraded: "warn",
  unavailable: "error",
};

function PanelHead({ id, kicker, title, note }: { id: string; kicker: string; title: ReactNode; note?: string }) {
  return (
    <div className="rv-obs-panel-heading rv-orbit-panel-heading">
      <div><small>{kicker}</small><h2 id={id}>{title}</h2></div>
      {note ? <span className="rv-obs-panel-note">{note}</span> : null}
    </div>
  );
}

function hasPoints(option: { series?: Array<{ data?: unknown[] }> }): boolean {
  return (option.series || []).some((series) => Array.isArray(series.data) && series.data.length > 0);
}

/*
 * A chart panel's body. ECharts draws bare axes for an empty series, so the
 * panel shows one calm line for loading, failure, and empty windows, and the
 * chart only when there is at least one sample to plot.
 */
function ChartArea({
  query,
  option,
  windowLabel,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; error?: unknown };
  option: { series?: Array<{ data?: unknown[] }> };
  windowLabel: string;
  children: ReactNode;
}) {
  if (hasPoints(option)) return <div className="rv-obs-chart-body">{children}</div>;
  if (query.isLoading) return <p className="rv-obs-chart-empty">Loading samples from Prometheus…</p>;
  if (query.isError) {
    return (
      <p className="rv-obs-chart-empty is-error">
        {isRoleRefusal(query.error) ? "This chart needs the operator or admin role." : "Prometheus did not answer this query. Use Refresh to try again."}
      </p>
    );
  }
  return <p className="rv-obs-chart-empty">No samples in the last {windowLabel}. Widen the range or clear the regex filters.</p>;
}
