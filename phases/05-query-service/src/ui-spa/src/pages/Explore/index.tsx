import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Chart from "../../components/Chart";
import { useOrbitTheme } from "../../components/OrbitTheme";
import { http } from "../../lib/api";
import { orbitChartAxis, orbitChartTooltip, readOrbitChartTheme } from "../../lib/chartTheme";
import { downloadCsv } from "../../lib/csv";
import { toLocalDatetimeInput } from "../../lib/datetime";
import { useSearchParams } from "react-router-dom";
import {
  Activity,
  Bookmark,
  CalendarRange,
  Copy,
  Database,
  Download,
  Play,
  RefreshCw,
  RotateCcw,
  TerminalSquare,
} from "lucide-react";
import { Button, SegmentedControl } from "@robot-vault/ui-kit";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import "./explore.css";

interface TSPoint {
  t: string;
  n: number;
  [column: string]: string | number | null | undefined;
}
interface TSSeriesPoint { t: string; value: number; n: number }
interface TSSeries {
  name: string;
  field: string;
  dimensions: Record<string, string>;
  points: TSSeriesPoint[];
}
interface TSResponse {
  points: TSPoint[];
  columns?: string[];
  all_null_columns?: string[];
  series?: TSSeries[];
  series_source?: string;
}
interface ChartSelection { series: string; timestamp: string; value: number }
interface SqlResp { columns?: string[]; rows?: any[]; }
interface SavedView {
  robot: string;
  topic: string | null;
  bucket: Bucket;
  since: string;
  until: string;
}
type ExploreMode = "stream" | "sql";
type Bucket = "auto" | "second" | "minute" | "hour" | "day";
interface ActionFeedback { tone: "success" | "error" | "info"; text: string }
interface QueryLimits {
  max_source_range_seconds: number;
  max_source_files: number;
  max_source_rows: number;
  max_source_bytes: number;
  query_timeout_seconds: number;
  max_concurrent_queries: number;
}
interface SchemaRange {
  ts_min: string | null;
  ts_max: string | null;
}

const SAVED_VIEWS_KEY = "robot-vault:explore-saved-views";
const SQL_HIST_KEY = "explore_sql_history";
const BUCKETS: readonly Bucket[] = ["auto", "second", "minute", "hour", "day"];

function isBucket(value: unknown): value is Bucket {
  return typeof value === "string" && (BUCKETS as readonly string[]).includes(value);
}

function isOptionalIsoDate(value: unknown): value is string {
  return typeof value === "string"
    && (value === "" || (value.length <= 64 && Number.isFinite(Date.parse(value))));
}

function isSavedView(value: unknown): value is SavedView {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.robot === "string"
    && (typeof candidate.topic === "string" || candidate.topic === null)
    && isBucket(candidate.bucket)
    && isOptionalIsoDate(candidate.since)
    && isOptionalIsoDate(candidate.until);
}

function pickBucketAuto(since?: string, until?: string): "second"|"minute"|"hour"|"day" {
  if (!since || !until) return "minute";
  const sec = (new Date(until).getTime() - new Date(since).getTime())/1000;
  if (sec <= 120) return "second";
  if (sec <= 7200) return "minute";
  if (sec <= 604800) return "hour";
  return "day";
}

export function queryErrorMessage(error: unknown, surface: ExploreMode = "sql"): string {
  let code: unknown;
  let detailRecord: Record<string, unknown> = {};
  if (error && typeof error === "object") {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") code = detail;
    else if (detail && typeof detail === "object") {
      detailRecord = detail as Record<string, unknown>;
      code = detailRecord.code;
    }
  }
  if (code === "result_limit_exceeded" && typeof detailRecord.max_rows === "number") {
    return `Result exceeds the ${detailRecord.max_rows.toLocaleString()} row limit. Narrow the query and retry.`;
  }
  if (code === "query_timeout") {
    return surface === "stream"
      ? "The stream query timed out. Shorten the time range and retry."
      : "The SQL query timed out. Simplify it, or switch to Stream mode and narrow the robot, topic, and time range.";
  }
  if (code === "query_capacity_exceeded") return "All query slots are busy. Wait a moment and retry.";
  if (code === "source_range_too_wide") {
    return surface === "stream"
      ? "The selected time range is too wide. Shorten From and Until, then retry."
      : "The catalog exceeds the SQL workspace's source-window limit. Switch to Stream mode and choose a narrower robot, topic, and time range.";
  }
  if (code === "source_range_reversed") {
    return surface === "stream"
      ? "From must be earlier than Until. Correct the time range and retry."
      : "The catalog reported a reversed source window. Switch to Stream mode and correct From and Until.";
  }
  if (code === "source_plan_unbounded") {
    return surface === "stream"
      ? "Set both From and Until so the source scan is bounded, then retry."
      : "The catalog cannot be admitted to the SQL workspace without measured bounds. Use Stream mode with a robot, topic, From, and Until.";
  }
  if (code === "source_file_limit_exceeded") return surface === "stream"
    ? "The query matches too many source files. Narrow the time range or robot/topic selection, then retry."
    : "The catalog exceeds the SQL workspace's source-file limit. Switch to Stream mode and narrow the robot, topic, and time range.";
  if (code === "source_row_limit_exceeded") return surface === "stream"
    ? "The query would scan too many source rows. Narrow the time range or filters, then retry."
    : "The catalog exceeds the SQL workspace's source-row limit. Switch to Stream mode and narrow the robot, topic, and time range.";
  if (code === "source_byte_limit_exceeded") return surface === "stream"
    ? "The query would scan too many source bytes. Narrow the time range or filters, then retry."
    : "The catalog exceeds the SQL workspace's source-byte limit. Switch to Stream mode and narrow the robot, topic, and time range.";
  if (code === "query_unavailable") return "The query engine is temporarily unavailable. Wait a moment and retry.";
  if (code === "query_invalid") return "The SQL query is not valid. Check its syntax and read-only source filters, then retry.";
  return "Query failed. Check the selection and retry.";
}

function loadSqlHistory(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(SQL_HIST_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => (
      typeof item === "string" && item.trim().length > 0 && item.length <= 20_000
    )).slice(0, 20);
  } catch {
    return [];
  }
}

function formatWindow(seconds: number): string {
  const units = [
    { seconds: 86_400, singular: "day" },
    { seconds: 3_600, singular: "hour" },
    { seconds: 60, singular: "minute" },
  ];
  const unit = units.find((candidate) => seconds >= candidate.seconds && seconds % candidate.seconds === 0);
  if (!unit) return `${seconds.toLocaleString()} seconds`;
  const count = seconds / unit.seconds;
  return `${count.toLocaleString()} ${unit.singular}${count === 1 ? "" : "s"}`;
}

function buildExploreSearch(
  robot: string,
  topic: string | null,
  bucket: Bucket,
  since: string,
  until: string,
  mode: ExploreMode,
): URLSearchParams {
  const next = new URLSearchParams();
  if (robot) next.set("robot", robot);
  if (topic) next.set("topic", topic);
  if (bucket !== "auto") next.set("bucket", bucket);
  if (since) next.set("since", since);
  if (until) next.set("until", until);
  if (mode === "sql") next.set("mode", "sql");
  return next;
}

export default function Explore() {
  const { theme } = useOrbitTheme();
  const chartTheme = useMemo(() => readOrbitChartTheme(), [theme]);
  const [params, setParams] = useSearchParams();
  const qpRobot = params.get("robot") || "";
  const qpTopic = params.get("topic");
  const qpBucket = params.get("bucket");
  const rawSince = params.get("since") || "";
  const rawUntil = params.get("until") || "";
  const qpSince = isOptionalIsoDate(rawSince) ? rawSince : "";
  const qpUntil = isOptionalIsoDate(rawUntil) ? rawUntil : "";
  const qpMode: ExploreMode = params.get("mode") === "sql" ? "sql" : "stream";
  const mode = qpMode;
  const searchString = params.toString();

  const [robot, setRobot] = useState<string>(qpRobot);
  const [topic, setTopic] = useState<string | null>(qpTopic || null);
  const [bucket, setBucket] = useState<Bucket>(isBucket(qpBucket) ? qpBucket : "auto");
  const [since, setSince] = useState<string>(qpSince);
  const [until, setUntil] = useState<string>(qpUntil);
  const [auto, setAuto] = useState<boolean>(false);
  const [chartSelection, setChartSelection] = useState<ChartSelection | null>(null);
  const [feedback, setFeedback] = useState<ActionFeedback | null>(null);
  const [copying, setCopying] = useState(false);
  const [allTimeBusy, setAllTimeBusy] = useState(false);
  const [savedViewEditorOpen, setSavedViewEditorOpen] = useState(false);
  const [savedViewDraft, setSavedViewDraft] = useState("");
  const [renamingView, setRenamingView] = useState<string | null>(null);
  const [pendingOverwrite, setPendingOverwrite] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [savedViews, setSavedViews] = useState<Record<string, SavedView>>(() => {
    try {
      const parsed: unknown = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "{}");
      if (!parsed || typeof parsed !== "object") return {};
      return Object.fromEntries(
        Object.entries(parsed as Record<string, unknown>).filter(([name, value]) => (
          name.trim().length > 0 && name.length <= 80 && isSavedView(value)
        ))
      ) as Record<string, SavedView>;
    } catch {
      return {};
    }
  });
  const incomingSearchRef = useRef<{ source: string; normalized: string } | null>(null);
  const lastWrittenSearchRef = useRef<string | null>(null);

  // Browser history and shared links are authoritative. The two refs prevent
  // an external POP/navigation and the local state-to-URL writer from
  // overwriting each other while React applies the incoming selection.
  useEffect(() => {
    if (lastWrittenSearchRef.current === searchString) {
      lastWrittenSearchRef.current = null;
      return;
    }
    const normalized = buildExploreSearch(qpRobot, qpTopic || null, isBucket(qpBucket) ? qpBucket : "auto", qpSince, qpUntil, qpMode).toString();
    incomingSearchRef.current = { source: searchString, normalized };
    setRobot(qpRobot);
    setTopic(qpTopic || null);
    setBucket(isBucket(qpBucket) ? qpBucket : "auto");
    setSince(qpSince);
    setUntil(qpUntil);
  }, [searchString]);

  // Robots for filter
  const robots = useQuery<string[]>({
    queryKey: ["robots"],
    queryFn: () => http<string[]>(`/api/robots`),
    staleTime: 60_000,
  });

  const topics = useQuery<string[]>({
    queryKey: ["message-types", robot],
    queryFn: () => http<string[]>(`/api/v1/message-types${robot ? `?robot_id=${encodeURIComponent(robot)}` : ""}`),
    staleTime: 60_000,
  });
  const topicIsValid = !!topic && topics.data?.includes(topic) === true;

  // Initialize a default topic and discard a stale topic when the robot changes.
  useEffect(() => {
    if (!topics.data) return;
    const nextTopic = topic && topics.data.includes(topic) ? topic : (topics.data[0] ?? null);
    if (nextTopic !== topic) setTopic(nextTopic);
  }, [topics.data, topic]);

  // Timeseries query
  const seriesQ = useQuery<TSResponse>({
    queryKey: ["timeseries", robot, topic, bucket, since, until],
    queryFn: () => {
      const search = new URLSearchParams();
      if (robot) search.set("robot_id", robot);
      if (topic) search.set("topic", topic);
      const b = bucket === "auto" ? pickBucketAuto(since, until) : bucket;
      if (since) search.set("since", since);
      if (until) search.set("until", until);
      search.set("bucket", b);
      return http<TSResponse>(`/api/timeseries?${search.toString()}`);
    },
    enabled: mode === "stream" && topicIsValid,
    retry: false,
  });

  // Recreate the timer for every active selection so it cannot keep
  // refetching an observer/key captured before the controls changed.
  useEffect(() => {
    if (!auto || mode !== "stream" || !topicIsValid) return;
    const timer = window.setInterval(() => { void seriesQ.refetch(); }, 10_000);
    return () => window.clearInterval(timer);
  }, [auto, mode, robot, topic, bucket, since, until, topicIsValid, seriesQ.refetch]);

  // ECharts option
  const option = useMemo(() => {
    const pts = seriesQ.data?.points ?? [];
    const typedSeries = (seriesQ.data?.series ?? []).filter((series) => series.points.length > 0);
    const numericColumns = (seriesQ.data?.columns ?? ["x", "y", "z"]).filter((column) =>
      pts.some((point) => typeof point[column] === "number" && Number.isFinite(point[column]))
    );
    const lineTypes = ["solid", "dashed", "dotted"] as const;
    const lineSeries = typedSeries.length > 0
      ? typedSeries.map((series, index) => ({
          name: series.name,
          type: "line",
          showSymbol: series.points.length <= 50,
          symbolSize: 7,
          lineStyle: { width: 2, type: lineTypes[index % lineTypes.length] },
          data: series.points.map((point) => [point.t, point.value]),
        }))
      : numericColumns.map((column, index) => ({
          name: column,
          type: "line",
          showSymbol: pts.length <= 50,
          symbolSize: 7,
          lineStyle: { width: 2, type: lineTypes[index % lineTypes.length] },
          data: pts.map((point) => [point.t, point[column] ?? null]),
        }));
    const legendNames = lineSeries.map((series) => series.name);
    const axis = orbitChartAxis(chartTheme);
    return {
      color: chartTheme.series,
      backgroundColor: "transparent",
      // No animated entrance (UI_DESIGN_GUIDELINES.md, Charts).
      animation: false,
      textStyle: { color: chartTheme.text },
      tooltip: orbitChartTooltip(chartTheme),
      // Pin the legend above the plot: this ECharts version places it at the
      // bottom by default, where the zoom slider covers it.
      legend: {
        type: "scroll",
        top: 0,
        left: "center",
        data: legendNames.length ? legendNames : ["count"],
        textStyle: { color: chartTheme.text },
        pageTextStyle: { color: chartTheme.text },
        pageIconColor: chartTheme.text,
        pageIconInactiveColor: chartTheme.axis,
      },
      xAxis: { type: "time", ...axis, axisLabel: { ...axis.axisLabel, hideOverlap: true } },
      yAxis: { type: "value", scale: true, ...axis },
      series: lineSeries.length ? lineSeries : [
        { name: "count", type: "bar", data: pts.map(p => [p.t, p.n ?? 0]) },
      ],
      grid: { left: 48, right: 20, top: 44, bottom: 72 },
      dataZoom: [
        { type: "inside", throttle: 80 },
        {
          type: "slider",
          bottom: 8,
          height: 24,
          borderColor: chartTheme.axis,
          backgroundColor: chartTheme.slider,
          fillerColor: chartTheme.selection,
          textStyle: { color: chartTheme.text },
          dataBackground: {
            lineStyle: { color: chartTheme.series[0] },
            areaStyle: { color: chartTheme.selection },
          },
        },
      ],
    } as any;
  }, [seriesQ.data, chartTheme]);

  const chartEvents = useMemo(() => ({
    click: (event: { seriesName?: string; value?: unknown }) => {
      if (!event.seriesName || !Array.isArray(event.value) || event.value.length < 2) return;
      const [timestamp, value] = event.value;
      if (typeof timestamp !== "string" || typeof value !== "number" || !Number.isFinite(value)) return;
      setChartSelection({ series: event.seriesName, timestamp, value });
    },
  }), []);

  useEffect(() => {
    setChartSelection(null);
  }, [robot, topic, bucket, since, until]);

  // URL sync
  useEffect(() => {
    const next = buildExploreSearch(robot, topic, bucket, since, until, mode);
    const nextString = next.toString();
    const incoming = incomingSearchRef.current;
    if (incoming?.source === searchString) {
      if (nextString !== incoming.normalized) return;
      incomingSearchRef.current = null;
    }
    if (nextString === searchString) return;
    lastWrittenSearchRef.current = nextString;
    setParams(next, { replace: true });
  }, [robot, topic, bucket, since, until, mode, searchString, setParams]);

  const selectMode = (nextMode: ExploreMode) => {
    const updated = new URLSearchParams(params);
    if (nextMode === "sql") updated.set("mode", "sql");
    else updated.delete("mode");
    setParams(updated);
  };

  // Copy link and Save view utilities
  const handleCopyLink = async () => {
    setCopying(true);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(window.location.href);
      setFeedback({ tone: "success", text: "Explore link copied." });
    } catch {
      setFeedback({ tone: "error", text: "Could not copy the link. Select the address from the browser instead." });
    } finally {
      setCopying(false);
    }
  };

  const persistSavedViews = (next: Record<string, SavedView>): boolean => {
    try {
      localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(next));
      setSavedViews(next);
      return true;
    } catch {
      setFeedback({ tone: "error", text: "This browser could not save the view locally." });
      return false;
    }
  };

  const closeSavedViewEditor = () => {
    setSavedViewEditorOpen(false);
    setSavedViewDraft("");
    setRenamingView(null);
    setPendingOverwrite(null);
  };

  const commitSavedView = (name: string) => {
    const next = { ...savedViews };
    const view = renamingView ? savedViews[renamingView] : { robot, topic, bucket, since, until };
    if (renamingView && renamingView !== name) delete next[renamingView];
    next[name] = view;
    if (!persistSavedViews(next)) return;
    setFeedback({
      tone: "success",
      text: renamingView ? `Saved view renamed to "${name}".` : `Saved view "${name}".`,
    });
    closeSavedViewEditor();
  };

  const handleSaveView = (event: FormEvent) => {
    event.preventDefault();
    const name = savedViewDraft.trim();
    if (!name) {
      setFeedback({ tone: "error", text: "Give the saved view a name." });
      return;
    }
    if (savedViews[name] && name !== renamingView) {
      setPendingOverwrite(name);
      return;
    }
    commitSavedView(name);
  };

  const startSavedView = () => {
    setSavedViewDraft("");
    setRenamingView(null);
    setPendingOverwrite(null);
    setPendingDelete(null);
    setSavedViewEditorOpen(true);
  };

  const startRenameView = (name: string) => {
    setSavedViewDraft(name);
    setRenamingView(name);
    setPendingOverwrite(null);
    setPendingDelete(null);
    setSavedViewEditorOpen(true);
  };

  const deleteSavedView = (name: string) => {
    const next = { ...savedViews };
    delete next[name];
    if (!persistSavedViews(next)) return;
    setPendingDelete(null);
    setFeedback({ tone: "success", text: `Deleted saved view "${name}".` });
  };
  const loadSavedView = (view: SavedView) => {
    setRobot(view.robot);
    setTopic(view.topic);
    setBucket(view.bucket);
    setSince(view.since);
    setUntil(view.until);
  };

  // CSV export of visible series
  const handleCsv = () => {
    try {
      const pts = seriesQ.data?.points ?? [];
      const typedSeries = seriesQ.data?.series ?? [];
      if (typedSeries.length) {
        const dimensionColumns = Array.from(new Set(
          typedSeries.flatMap((series) => Object.keys(series.dimensions))
        ));
        downloadCsv(
          "explore.csv",
          ["t", "series", "field", ...dimensionColumns, "value", "count"],
          typedSeries.flatMap((series) => series.points.map((point) => [
            point.t,
            series.name,
            series.field,
            ...dimensionColumns.map((column) => series.dimensions[column]),
            point.value,
            point.n,
          ])),
        );
        setFeedback({ tone: "success", text: `Downloaded ${typedSeries.reduce((count, series) => count + series.points.length, 0).toLocaleString()} plotted points.` });
        return;
      }
      const numericColumns = (seriesQ.data?.columns ?? ["x", "y", "z"]).filter((column) =>
        pts.some((point) => typeof point[column] === "number" && Number.isFinite(point[column]))
      );
      downloadCsv(
        "explore.csv",
        numericColumns.length ? ["t", ...numericColumns] : ["t", "count"],
        pts.map((point) => numericColumns.length
          ? [point.t, ...numericColumns.map((column) => point[column])]
          : [point.t, point.n]),
      );
      setFeedback({ tone: "success", text: `Downloaded ${pts.length.toLocaleString()} plotted points.` });
    } catch {
      setFeedback({ tone: "error", text: "Could not create the CSV file." });
    }
  };

  // All-time range helper via registry metadata and the server's live limit.
  const handleAllTime = async () => {
    if (!robot || !topic) {
      setFeedback({ tone: "error", text: "Choose a robot and topic before loading the all-time range." });
      return;
    }
    setAllTimeBusy(true);
    try {
      const schemaParams = new URLSearchParams({ robot_id: robot, topic });
      const [schema, limits] = await Promise.all([
        http<SchemaRange>(`/api/v1/schema?${schemaParams.toString()}`),
        http<QueryLimits>("/api/v1/query/limits"),
      ]);
      if (!Number.isFinite(limits.max_source_range_seconds) || limits.max_source_range_seconds <= 0) {
        throw new Error("invalid query limits");
      }
      if (schema.ts_min && schema.ts_max) {
        const tmin = schema.ts_min;
        const tmax = schema.ts_max;
          let down = new Date(tmin); down.setSeconds(0,0);
          const up = new Date(tmax);
          const hasPartialMinute = up.getSeconds() !== 0 || up.getMilliseconds() !== 0;
          up.setSeconds(0, 0);
          if (hasPartialMinute) up.setMinutes(up.getMinutes() + 1);
          if (up <= down) up.setMinutes(up.getMinutes()+1);
          const maxWindowMs = limits.max_source_range_seconds * 1_000;
          const clamped = up.getTime() - down.getTime() > maxWindowMs;
          if (clamped) down = new Date(up.getTime() - maxWindowMs);
          setSince(down.toISOString());
          setUntil(up.toISOString());
          setFeedback(clamped
            ? { tone: "info", text: `Recorded history exceeds the current query limit. Loaded the latest ${formatWindow(limits.max_source_range_seconds)} instead.` }
            : { tone: "success", text: "Loaded the full recorded range for this stream." });
          return;
      }
      setFeedback({ tone: "info", text: "No recorded range was returned for this stream." });
    } catch {
      setFeedback({ tone: "error", text: "Could not load the all-time range. Try again." });
    } finally {
      setAllTimeBusy(false);
    }
  };

  // SQL console
  const [sql, setSql] = useState<string>("SELECT topic, count(*) AS n FROM all_parquet GROUP BY 1 ORDER BY 2 DESC LIMIT 20;");
  const [sqlErr, setSqlErr] = useState<string>("");
  const [sqlRes, setSqlRes] = useState<SqlResp>({});
  const [sqlBusy, setSqlBusy] = useState(false);
  const saveSqlHistory = (text: string) => {
    const trimmed = (text || "").trim(); if (!trimmed) return;
    try {
      const current = loadSqlHistory();
      const next = [trimmed.slice(0, 20_000), ...current.filter((s) => s !== trimmed)].slice(0, 20);
      localStorage.setItem(SQL_HIST_KEY, JSON.stringify(next));
    } catch {}
  };
  const [hist, setHist] = useState<string[]>(loadSqlHistory());
  const runSql = async (query = sql) => {
    if (sqlBusy || !query.trim()) return;
    setSqlBusy(true);
    setSqlErr(""); setSqlRes({}); setFeedback(null);
    try {
      // No `headers` override: see the comment on the /api/v1/s3/presign
      // call in Uploads/index.tsx -- this had the same duplicate
      // Content-Type bug, which surfaced as this endpoint's own 422 the
      // instant it started requiring a real (non-empty) request body to
      // reach past auth.
      const res = await http<SqlResp>(`/api/v1/query/sql`, { method: "POST", body: JSON.stringify({ sql: query }) } as any);
      setSqlRes(res);
      saveSqlHistory(query); setHist(loadSqlHistory());
      setFeedback({ tone: "success", text: `Query returned ${(res.rows?.length ?? 0).toLocaleString()} rows.` });
    } catch (error) {
      const message = queryErrorMessage(error, "sql");
      setSqlErr(message);
    } finally {
      setSqlBusy(false);
    }
  };

  const handleSqlKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      void runSql();
    }
  };

  const handleSqlCsv = () => {
    try {
      const cols = sqlRes.columns || [];
      const rows = sqlRes.rows || [];
      if (!cols.length) return;
      downloadCsv("sql_results.csv", cols, rows);
      setFeedback({ tone: "success", text: `Downloaded ${rows.length.toLocaleString()} SQL result rows.` });
    } catch {
      setFeedback({ tone: "error", text: "Could not create the SQL results CSV file." });
    }
  };

  // Chart ref for reset zoom
  const chartRef = useRef<any>(null);

  const bucketEffective = bucket === "auto" ? pickBucketAuto(since, until) : (bucket as any);
  const hasTypedSeries = (seriesQ.data?.series ?? []).some((series) => series.points.length > 0);
  const points = seriesQ.data?.points ?? [];
  const hasChartData = hasTypedSeries || points.length > 0;
  const responseColumns = seriesQ.data?.columns ?? [];
  const hasOnlyNullPayload = !hasTypedSeries
    && points.length > 0
    && responseColumns.length > 0
    && responseColumns.every((column) => seriesQ.data?.all_null_columns?.includes(column));
  const chartAnnouncement = chartSelection
    ? `Selected ${chartSelection.series} at ${chartSelection.timestamp}: ${chartSelection.value}`
    : "";

  const plottedSeries = Array.isArray(option.series) ? option.series.length : 0;
  const plottedPoints = hasTypedSeries
    ? (seriesQ.data?.series ?? []).reduce((count, series) => count + series.points.length, 0)
    : points.length;
  const timeZone = (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "local"; } catch { return "local"; }
  })();
  const rangeLabel = `${since ? toLocalDatetimeInput(since).replace("T", " ") : "Open"} → ${until ? toLocalDatetimeInput(until).replace("T", " ") : "Now"}`;
  const sqlColumns = sqlRes.columns ?? [];
  const sqlRows = sqlRes.rows ?? [];
  const numericSqlColumns = sqlColumns.map((_, index) => (
    sqlRows.length > 0 && sqlRows.every((row) => row?.[index] == null || typeof row[index] === "number")
      && sqlRows.some((row) => typeof row?.[index] === "number")
  ));
  const resetZoom = () => {
    try { chartRef.current?.getEchartsInstance()?.dispatchAction({ type: "dataZoom", start: 0, end: 100 }); } catch { /* chart can be between renders */ }
    setChartSelection(null);
    setFeedback({ tone: "success", text: "Chart zoom reset." });
  };
  const deleteHistoryEntry = (query: string) => {
    try {
      const newHist = hist.filter((entry) => entry !== query);
      localStorage.setItem(SQL_HIST_KEY, JSON.stringify(newHist));
      setHist(newHist);
      setFeedback({ tone: "success", text: "Deleted query from local history." });
    } catch {
      setFeedback({ tone: "error", text: "Could not update local query history." });
    }
  };

  return (
    <div className="rv-explore">
      <PageHeader
        kicker="02 · Explore"
        title="Explore"
        description="Inspect synchronized robot streams, or query the Parquet catalog with read-only SQL."
        actions={
          <>
            <PageStatus>Read-only</PageStatus>
            <SegmentedControl<ExploreMode>
              className="rv-explore__mode"
              label="Explore workspace"
              value={mode}
              onChange={selectMode}
              options={[
                { value: "stream", label: "Stream" },
                { value: "sql", label: "SQL" },
              ]}
            />
          </>
        }
      />

      {feedback ? (
        <div className={`rv-explore__feedback is-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"} aria-live={feedback.tone === "error" ? "assertive" : "polite"}>
          {feedback.text}
        </div>
      ) : null}

      {mode === "stream" ? (
        <>
          <section id="explore-stream" className="rv-explore__filters" aria-labelledby="rv-explore-query-title">
            <h2 id="rv-explore-query-title" className="rv-visually-hidden">Select a stream</h2>
            <div className="rv-explore__fields">
              <label className="rv-explore__field is-robot"><span>Robot</span><select value={robot} onChange={(e) => { setRobot(e.target.value); setTopic(null); }} disabled={robots.isLoading}>
                <option value="">(all robots)</option>
                {(robots.data || []).map((r) => (<option key={r} value={r}>{r}</option>))}
              </select></label>
              <label className="rv-explore__field is-topic"><span>Topic</span><select value={topic || ""} onChange={(e) => setTopic(e.target.value || null)} disabled={topics.isLoading || (topics.data?.length || 0) === 0}>
                <option value="" disabled>Select topic…</option>
                {(topics.data || []).map((t) => <option key={t} value={t}>{t}</option>)}
              </select></label>
              <label className="rv-explore__field is-date"><span>From</span><input type="datetime-local" value={toLocalDatetimeInput(since)} onChange={(e)=> setSince(e.target.value ? new Date(e.target.value).toISOString() : "")} /></label>
              <label className="rv-explore__field is-date"><span>Until</span><input type="datetime-local" value={toLocalDatetimeInput(until)} onChange={(e)=> setUntil(e.target.value ? new Date(e.target.value).toISOString() : "")} /></label>
              <label className="rv-explore__field is-bucket"><span>Bucket</span><select value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)}>{BUCKETS.map(b => <option key={b} value={b}>{b}</option>)}</select></label>
            </div>
            {robots.isError ? (
              <div className="rv-explore__source-error" role="alert">
                <span>Could not load the robot list. Check the connection and retry.</span>
                <Button variant="secondary" size="sm" className="rv-explore__btn" disabled={robots.isFetching} onClick={() => void robots.refetch()}>
                  {robots.isFetching ? "Retrying…" : "Retry robot list"}
                </Button>
              </div>
            ) : null}
            <div className="rv-explore__toolbar">
              <div className="rv-explore__toolbar-group">
                <Button size="sm" className="rv-explore__btn is-primary" onClick={() => { if (topicIsValid) void seriesQ.refetch(); }} disabled={!topicIsValid || seriesQ.isFetching} aria-busy={seriesQ.isFetching}>
                  <RefreshCw size={14} aria-hidden="true" /> {seriesQ.isFetching ? "Refreshing…" : "Refresh"}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  className="rv-explore__btn"
                  onClick={() => void handleAllTime()}
                  disabled={allTimeBusy || !robot || !topicIsValid}
                  aria-busy={allTimeBusy}
                  title={!robot || !topicIsValid ? "Choose a robot and valid topic first" : undefined}
                ><CalendarRange size={14} aria-hidden="true" /> {allTimeBusy ? "Loading range…" : "All-time"}</Button>
              </div>
              <div className="rv-explore__toolbar-group">
                <Button variant="secondary" size="sm" className="rv-explore__btn" onClick={handleCsv} disabled={!hasChartData}><Download size={14} aria-hidden="true" /> CSV</Button>
                <Button variant="secondary" size="sm" className="rv-explore__btn" onClick={() => void handleCopyLink()} disabled={copying}><Copy size={14} aria-hidden="true" /> {copying ? "Copying…" : "Copy link"}</Button>
                <Button variant="secondary" size="sm" className="rv-explore__btn" onClick={startSavedView}><Bookmark size={14} aria-hidden="true" /> Save view</Button>
                <Button variant="secondary" size="sm" className="rv-explore__btn" onClick={resetZoom} disabled={!hasChartData}><RotateCcw size={14} aria-hidden="true" /> Reset zoom</Button>
              </div>
              <label className="rv-explore__auto"><input type="checkbox" checked={auto} onChange={(e)=> setAuto(e.target.checked)} /><span>Auto-refresh</span></label>
            </div>

            {savedViewEditorOpen ? (
              <form className="rv-explore__save-form" onSubmit={handleSaveView}>
                <label htmlFor="saved-view-name">{renamingView ? "Rename saved view" : "Saved view name"}</label>
                <input id="saved-view-name" autoFocus value={savedViewDraft} maxLength={80} onChange={(event) => { setSavedViewDraft(event.target.value); setPendingOverwrite(null); }} />
                <Button size="sm" className="rv-explore__btn is-primary" type="submit">{renamingView ? "Save name" : "Save"}</Button>
                <Button variant="secondary" size="sm" className="rv-explore__btn" type="button" onClick={closeSavedViewEditor}>Cancel</Button>
                {pendingOverwrite ? (
                  <div className="rv-explore__confirm" role="alert">
                    <span>A view named "{pendingOverwrite}" already exists.</span>
                    <Button variant="secondary" size="sm" className="rv-explore__btn" type="button" onClick={() => commitSavedView(pendingOverwrite)}>Overwrite view</Button>
                    <Button variant="secondary" size="sm" className="rv-explore__btn" type="button" onClick={() => setPendingOverwrite(null)}>Keep existing</Button>
                  </div>
                ) : null}
              </form>
            ) : null}

            {Object.keys(savedViews).length ? (
              <div role="group" className="rv-explore__saved" aria-label="Saved Explore views">
                <span className="rv-explore__saved-label"><Bookmark size={14} aria-hidden="true" /> Saved views</span>
                {Object.entries(savedViews).map(([name, view]) => (
                  <div className="rv-explore__saved-item" key={name}>
                    <button className="rv-explore__saved-name" type="button" onClick={() => { loadSavedView(view); setFeedback({ tone: "success", text: `Loaded saved view "${name}".` }); }}>{name}</button>
                    <button type="button" onClick={() => startRenameView(name)} aria-label={`Rename ${name}`}>Rename</button>
                    {pendingDelete === name ? (
                      <>
                        <button className="is-danger" type="button" onClick={() => deleteSavedView(name)} aria-label={`Confirm delete ${name}`}>Confirm delete</button>
                        <button type="button" onClick={() => setPendingDelete(null)} aria-label={`Cancel delete ${name}`}>Keep</button>
                      </>
                    ) : (
                      <button type="button" onClick={() => { setPendingDelete(name); setSavedViewEditorOpen(false); }} aria-label={`Delete ${name}`}>Delete</button>
                    )}
                  </div>
                ))}
              </div>
            ) : null}
          </section>

          <section id="explore-signal" className="rv-orbit-panel rv-explore__panel rv-explore__signal" aria-labelledby="rv-explore-chart-title">
            <div className="rv-explore__panel-head">
              <div>
                <p className="rv-explore__kicker">Time series</p>
                <h2 id="rv-explore-chart-title">Stream activity</h2>
              </div>
              {!!topic && seriesQ.data && hasChartData ? (
                <span className="rv-explore__meta">
                  {plottedSeries.toLocaleString()} series · {plottedPoints.toLocaleString()} points
                </span>
              ) : <Activity size={16} aria-hidden="true" />}
            </div>

            <dl className="rv-explore__summary" aria-label="Current Explore selection">
              <div>
                <dt>Robot</dt>
                <dd title={robot || "All robots"}>{robot || "All robots"}</dd>
                <small>{robot ? "Filtered to one robot" : "No robot filter"}</small>
              </div>
              <div>
                <dt>Topic</dt>
                <dd title={topic || "Not selected"}>{topic || "Not selected"}</dd>
                <small>{topicIsValid ? "Message type" : "Choose a topic to plot"}</small>
              </div>
              <div>
                <dt>Range</dt>
                <dd title={rangeLabel}>{rangeLabel}</dd>
                <small>{`Local time · ${timeZone}`}</small>
              </div>
              <div>
                <dt>Bucket</dt>
                <dd>{bucketEffective}</dd>
                <small>{bucket === "auto" ? "Automatic" : "Set manually"}</small>
              </div>
            </dl>

            <div className="rv-explore__chart-body">
              {topics.isLoading && <ExploreState title="Loading topics…" />}
              {topics.isError && <ExploreState tone="error" title="Could not load topics." hint="Refresh the page to retry." />}
              {topics.data && topics.data.length === 0 && (
                <ExploreState
                  title="No topics found."
                  hint={robot ? "Choose another robot, or clear the robot filter." : "Topics appear here after a capture is processed."}
                />
              )}
              {!!topic && seriesQ.isLoading && <ExploreState title="Loading time series…" />}
              {!!topic && seriesQ.isError && <ExploreState tone="error" role="alert" title={queryErrorMessage(seriesQ.error, "stream")} />}
              {!!topic && seriesQ.data && !hasChartData && (
                <ExploreState
                  title="No points in the selected range."
                  hint={since || until
                    ? "Widen From and Until, or use All-time to load the full recorded range."
                    : "Choose another topic or robot, or use All-time to load the recorded range."}
                />
              )}
              {!!topic && seriesQ.data && hasOnlyNullPayload && (
                <p className="rv-explore__note" role="note">
                  This topic has rows, but its numeric fields ({responseColumns.join(", ")}) are empty. Showing message counts.
                </p>
              )}
              {!!topic && seriesQ.data && hasChartData && (
                <>
                  <Chart
                    ref={chartRef as any}
                    option={option}
                    onEvents={chartEvents}
                    style={{ height: 360 }}
                    accessibility={{
                      title: `${topic} time series`,
                      summary: `Plotted values for ${robot || "all robots"} in the selected window.`,
                      tableLabel: `${topic} plotted values`,
                      truncatedMessage: "Showing a balanced sample of plotted points. Use CSV for the complete stream result.",
                    }}
                    selectionAnnouncement={chartAnnouncement}
                  />
                  {chartSelection ? <div className="rv-explore__selection">{chartAnnouncement}</div> : null}
                </>
              )}
            </div>
          </section>
        </>
      ) : (
        <div className="rv-explore__sql">
          <div className="rv-explore__sql-main">
            <section id="explore-query" className="rv-orbit-panel rv-explore__panel" aria-labelledby="rv-explore-console-title">
              <div className="rv-explore__panel-head">
                <div>
                  <p className="rv-explore__kicker">DuckDB · read-only</p>
                  <h2 id="rv-explore-console-title">SQL console</h2>
                </div>
                <TerminalSquare size={16} aria-hidden="true" />
              </div>
              <div className="rv-explore__console">
                <label className="rv-visually-hidden" htmlFor="explore-sql">SQL query</label>
                <textarea id="explore-sql" spellCheck={false} value={sql} onChange={(e)=> setSql(e.target.value)} onKeyDown={handleSqlKeyDown} aria-keyshortcuts="Control+Enter Meta+Enter" disabled={sqlBusy} />
                <div className="rv-explore__toolbar">
                  <div className="rv-explore__toolbar-group">
                    <Button size="sm" className="rv-explore__btn is-primary" onClick={() => void runSql()} disabled={sqlBusy || !sql.trim()} aria-busy={sqlBusy} aria-keyshortcuts="Control+Enter Meta+Enter">
                      <Play size={14} aria-hidden="true" /> {sqlBusy ? "Running…" : "Run"}
                    </Button>
                    <Button variant="secondary" size="sm" className="rv-explore__btn" onClick={handleSqlCsv} disabled={!sqlRes.columns?.length}><Download size={14} aria-hidden="true" /> CSV results</Button>
                  </div>
                  <span className="rv-explore__shortcut"><kbd>Ctrl</kbd> or <kbd>Cmd</kbd> + <kbd>Enter</kbd> runs the query</span>
                </div>
                {!!sqlErr && <div className="rv-explore__sql-error" role="alert">{sqlErr}</div>}
              </div>
            </section>

            <section className="rv-orbit-panel rv-explore__panel" aria-labelledby="rv-explore-results-title" aria-busy={sqlBusy}>
              <div className="rv-explore__panel-head">
                <div>
                  <p className="rv-explore__kicker">Result</p>
                  <h2 id="rv-explore-results-title">Query results</h2>
                </div>
                {sqlColumns.length ? (
                  <span className="rv-explore__meta">
                    {sqlRows.length.toLocaleString()} {sqlRows.length === 1 ? "row" : "rows"} · {sqlColumns.length.toLocaleString()} {sqlColumns.length === 1 ? "column" : "columns"}
                  </span>
                ) : null}
              </div>
              <div className="rv-explore__results">
                {sqlColumns.length === 0 ? (
                  sqlBusy
                    ? <ExploreState title="Running query…" />
                    : <ExploreState title="No results yet." hint="Run a query to see its rows here." />
                ) : (
                  <table>
                    <caption className="rv-visually-hidden">SQL query results</caption>
                    <thead><tr>{sqlColumns.map((c, i) => <th scope="col" key={i} className={numericSqlColumns[i] ? "is-num" : undefined}>{c}</th>)}</tr></thead>
                    <tbody>{sqlRows.map((r, ri) => <tr key={ri}>{(r || []).map((v: any, ci: number) => <td key={ci} className={numericSqlColumns[ci] ? "is-num" : undefined}>{v == null ? "" : String(v)}</td>)}</tr>)}</tbody>
                  </table>
                )}
              </div>
            </section>
          </div>

          <section id="explore-history" className="rv-orbit-panel rv-explore__panel rv-explore__history" aria-labelledby="rv-explore-history-title">
            <div className="rv-explore__panel-head">
              <div>
                <p className="rv-explore__kicker">This browser</p>
                <h2 id="rv-explore-history-title">Recent queries</h2>
              </div>
              {hist.length ? <span className="rv-explore__meta">{hist.length} {hist.length === 1 ? "query" : "queries"}</span> : <Database size={16} aria-hidden="true" />}
            </div>
            {hist.length === 0 ? (
              <ExploreState title="No query history yet." hint="Queries you run are kept in this browser." />
            ) : (
              <ol className="rv-explore__history-list">
                {hist.map((q, idx) => (
                  <li key={idx}>
                    <code title={q}>{q}</code>
                    <div>
                      <button type="button" onClick={() => setSql(q)} disabled={sqlBusy}>Load</button>
                      <button type="button" onClick={() => { setSql(q); void runSql(q); }} disabled={sqlBusy}>Run</button>
                      <button type="button" onClick={() => deleteHistoryEntry(q)} disabled={sqlBusy}>Delete</button>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function ExploreState({
  title,
  hint,
  tone,
  role,
}: {
  title: string;
  hint?: string;
  tone?: "error";
  role?: "alert";
}) {
  return (
    <div className={`rv-explore__state${tone === "error" ? " is-error" : ""}`} role={role}>
      <p className="rv-explore__state-title">{title}</p>
      {hint ? <p className="rv-explore__state-hint">{hint}</p> : null}
    </div>
  );
}
