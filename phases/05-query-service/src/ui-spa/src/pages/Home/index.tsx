import { ReactNode, SVGProps, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Bot, RefreshCw } from "lucide-react";
import {
  Button,
  EmptyState,
  Skeleton,
  StatusPill,
  Table,
  TD,
  TH,
  THead,
  TR,
  cn,
} from "@robot-vault/ui-kit";
import { http } from "../../lib/api";
import { useEditionQuery, useIsProEdition, useObsHealthy } from "../../components/Layout";
import { useOnboarding } from "../../components/OnboardingContext";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import { pro } from "../../pro";
import { NO_HOME_ATTENTION, type HomeAttentionState } from "../../lib/proContract";
import { AttentionRow, Chip, relativeTime, type ChipTone } from "./parts";
import "./Home.css";

/*
 * The operator's landing page: what this vault is, what the fleet has been
 * doing, and whether the services behind the numbers are actually answering.
 *
 * Everything on this page is a real field from a real endpoint:
 *   GET /api/v1/edition           -> { edition: "core" | "pro" }
 *   GET /api/tenant               -> { scope: <S3_PREFIX_SCOPE> }
 *   GET /api/v1/metrics/overview  -> { kpis: { uploads_last_hour,
 *                                              storage_bytes_total,
 *                                              episodes_awaiting_qa,
 *                                              exports_completed_24h } }
 *   GET /api/v1/robots/list       -> { items: [...] }
 *   GET /api/v1/robots/{id}/summary -> item + topics[] + recent_uploads[]
 *
 * There is deliberately NO version chip: the query service exposes no version
 * or build endpoint (`main.py` registers `/`, `/healthz`, `/readyz`, `/metrics`,
 * `/api/tenant`, `/api/v1/edition`, and `/api/selfcheck` and nothing else),
 * and a number invented for the header would be worse than no number.
 *
 * Pipeline-flow metrics are Postgres-backed, not
 * Prometheus-backed: `/api/v1/metrics/overview` reads `uploads`/`episodes`/
 * `qa_reports`/`export_status` directly, the same system of record every
 * other widget on this page reads, so it is populated in every deployment --
 * `obs` profile running or not. It still degrades on its own: a null KPI
 * (an un-migrated DB, or a core edition's absent `episodes`/`qa_reports`
 * tables) renders as "—", and the degraded banner below only fires when the
 * endpoint itself failed to answer, i.e. the query service could not reach
 * Postgres at all. This page does not use Prometheus or Grafana; see the
 * opt-in `/ui/obs` page, linked from the nav when the `obs` profile is
 * actually up (see `Layout.useObsHealthy`).
 */

interface MetricsOverview {
  kpis: {
    uploads_last_hour: number | null;
    storage_bytes_total: number | null;
    episodes_awaiting_qa: number | null;
    exports_completed_24h: number | null;
  };
}

interface RobotListItem {
  robot_id: string;
  display_name: string | null;
  first_seen: string | null;
  last_upload_at: string | null;
  total_uploads: number;
  completed_uploads: number;
  failed_uploads: number;
  uploading: number;
  total_bytes: number;
}

interface RobotSummary extends RobotListItem {
  topics: { topic: string; uploads: number; rows_written: number }[];
  recent_uploads: {
    upload_id: string;
    original_filename: string | null;
    status: string;
    file_size_bytes: number;
    created_at: string | null;
  }[];
}

// ---------------------------------------------------------------------------
// shared parts: chip, attention row
// ---------------------------------------------------------------------------

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** `fleet.py` serialises TIMESTAMPTZ as ISO-8601 UTC with a trailing `Z`. */
function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

type FleetNodeState = "active" | "uploading" | "review" | "stale" | "inactive";

const STATE_TONE: Record<FleetNodeState, ChipTone> = {
  active: "ok",
  uploading: "info",
  review: "bad",
  stale: "warn",
  inactive: "neutral",
};

function fleetNodeState(robot: RobotListItem): { state: FleetNodeState; label: string } {
  if (robot.failed_uploads > 0) return { state: "review", label: "Needs review" };
  if (robot.uploading > 0) return { state: "uploading", label: "Uploading" };
  if (!robot.last_upload_at) return { state: "inactive", label: "No uploads" };
  const lastUpload = Date.parse(robot.last_upload_at);
  if (!Number.isFinite(lastUpload) || Date.now() - lastUpload > 7 * 24 * 3600_000) {
    return { state: "stale", label: "Stale" };
  }
  return { state: "active", label: "Active" };
}

function ChevronIcon({ open, ...props }: SVGProps<SVGSVGElement> & { open?: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d={open ? "M4 6l4 4 4-4" : "M6 4l4 4-4 4"} />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// pipeline flow strip
// ---------------------------------------------------------------------------

/**
 * One stage of the "Pipeline flow" strip. The metric is a Postgres KPI from
 * `/api/v1/metrics/overview`; `facts` are only figures this page already
 * holds (robot registry totals, dataset recovery items), never derived
 * estimates.
 */
function FlowStage({
  node,
  stage,
  label,
  to,
  value,
  format,
  hint,
  loading,
  emphasis,
  facts,
}: {
  /** Stage number shown in the node, or `null` for the unnumbered storage step. */
  node: string | null;
  stage: string;
  label: string;
  /** The stage's page, when this deployment has one. */
  to?: string;
  value: number | null | undefined;
  /** Defaults to `value.toLocaleString()`; pass e.g. `formatBytes` for a
   * metric whose unit is part of the formatted string. */
  format?: (v: number) => string;
  hint: string;
  loading: boolean;
  /** Tints a non-zero metric (e.g. episodes waiting for QA). */
  emphasis?: "warn";
  facts?: ReactNode;
}) {
  return (
    <div className="rv-home__stage" role="group" aria-label={node ? `${node} ${stage}` : stage}>
      <div className="rv-home__stage-head">
        <span className={cn("rv-home__node", !node && "is-pending")} aria-hidden="true">{node ?? "··"}</span>
        {to ? (
          <Link className="rv-home__stage-label is-link" to={to}>
            {label}
            <ArrowRight size={14} aria-hidden="true" />
          </Link>
        ) : (
          <span className="rv-home__stage-label">{label}</span>
        )}
      </div>
      <div className={cn("rv-home__metric", emphasis && value != null && value > 0 && `is-${emphasis}`)}>
        {loading ? (
          <Skeleton className="h-8 w-20" />
        ) : value == null ? (
          <span className="rv-home__metric-empty" title="No data yet for this metric">—</span>
        ) : (
          format ? format(value) : value.toLocaleString()
        )}
      </div>
      <span className="rv-home__stage-hint">{hint}</span>
      {facts ? <div className="rv-home__stage-facts">{facts}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// fleet
// ---------------------------------------------------------------------------

function RobotDetail({ robotId }: { robotId: string }) {
  const summary = useQuery<RobotSummary>({
    queryKey: ["robot-summary", robotId],
    queryFn: () => http<RobotSummary>(`/api/v1/robots/${encodeURIComponent(robotId)}/summary`),
    staleTime: 30_000,
  });

  if (summary.isLoading) {
    return (
      <div className="space-y-2 py-1">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-3 animate-pulse rounded bg-muted-foreground/15"
            style={{ width: `${80 - i * 20}%` }}
          />
        ))}
      </div>
    );
  }

  if (summary.isError || !summary.data) {
    return (
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm text-muted-foreground">
          Could not load the summary for <code>{robotId}</code>.
        </span>
        <Button size="sm" variant="secondary" onClick={() => summary.refetch()}>Retry</Button>
      </div>
    );
  }

  const data = summary.data;
  const topics = data.topics ?? [];
  const recent = data.recent_uploads ?? [];
  const shownTopics = topics.slice(0, 8);

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Recent uploads
        </h4>
        {recent.length === 0 ? (
          <p className="text-sm text-muted-foreground">This robot has never uploaded.</p>
        ) : (
          <ul className="space-y-1.5">
            {recent.map((u) => (
              <li key={u.upload_id} className="flex items-center gap-2 text-sm">
                <StatusPill status={u.status} />
                <span className="truncate" title={u.original_filename ?? u.upload_id}>
                  {u.original_filename || u.upload_id}
                </span>
                <span className="ml-auto shrink-0 tabular-nums text-xs text-muted-foreground">
                  {formatBytes(u.file_size_bytes)} · {relativeTime(u.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Topics processed
        </h4>
        {topics.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing processed yet — topics appear once the pipeline writes Parquet for an upload.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {shownTopics.map((t) => (
              <li key={t.topic} className="flex items-baseline gap-2 text-sm">
                <code className="truncate">{t.topic}</code>
                <span className="ml-auto shrink-0 tabular-nums text-xs text-muted-foreground">
                  {t.rows_written.toLocaleString()} rows · {t.uploads} upload{t.uploads === 1 ? "" : "s"}
                </span>
              </li>
            ))}
            {topics.length > shownTopics.length && (
              <li className="text-xs text-muted-foreground">
                +{topics.length - shownTopics.length} more topic
                {topics.length - shownTopics.length === 1 ? "" : "s"}
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  );
}

function FleetRow({
  robot,
  isPro,
  expanded,
  onToggle,
}: {
  robot: RobotListItem;
  isPro: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  const panelId = `robot-panel-${robot.robot_id}`;
  const status = fleetNodeState(robot);
  return (
    <>
      <TR
        className={cn("rv-home__fleet-row cursor-pointer", expanded && "is-expanded")}
        onClick={onToggle}
      >
        <TD>
          <div className="rv-home__robot">
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={panelId}
              aria-label={`${expanded ? "Collapse" : "Expand"} ${robot.robot_id}`}
              className="rv-home__disclosure"
              onClick={(e) => { e.stopPropagation(); onToggle(); }}
            >
              <ChevronIcon open={expanded} />
            </button>
            <span className="rv-home__robot-name">{robot.robot_id}</span>
            {robot.display_name && robot.display_name !== robot.robot_id ? (
              <span className="rv-home__robot-alias">{robot.display_name}</span>
            ) : null}
          </div>
        </TD>
        <TD><Chip tone={STATE_TONE[status.state]}>{status.label}</Chip></TD>
        <TD className="rv-home__mono" title={robot.last_upload_at ?? "never"}>
          {relativeTime(robot.last_upload_at)}
        </TD>
        <TD className="rv-home__num">{robot.total_uploads.toLocaleString()}</TD>
        <TD className="rv-home__num">{robot.completed_uploads.toLocaleString()}</TD>
        <TD className={cn("rv-home__num", robot.failed_uploads > 0 ? "is-bad" : "is-zero")}>
          {robot.failed_uploads.toLocaleString()}
        </TD>
        <TD className={cn("rv-home__num", robot.uploading > 0 ? "is-info" : "is-zero")}>
          {robot.uploading.toLocaleString()}
        </TD>
        <TD className="rv-home__num">{formatBytes(robot.total_bytes)}</TD>
        <TD onClick={(e) => e.stopPropagation()}>
          <span className="rv-home__open-links">
            {/* Filter keys match each destination page's own URL state:
                Uploads/Episodes read `robot_id`, Explore reads `robot`. */}
            <Link className="rv-home__link" to={`/uploads?robot_id=${encodeURIComponent(robot.robot_id)}`}>
              Uploads
            </Link>
            {isPro && (
              <Link className="rv-home__link" to={`/episodes?robot_id=${encodeURIComponent(robot.robot_id)}`}>
                Episodes
              </Link>
            )}
            <Link className="rv-home__link" to={`/explore?robot=${encodeURIComponent(robot.robot_id)}`}>
              Explore
            </Link>
          </span>
        </TD>
      </TR>
      {expanded && (
        <TR className="rv-home__detail-row">
          <TD id={panelId} colSpan={9}>
            <RobotDetail robotId={robot.robot_id} />
          </TD>
        </TR>
      )}
    </>
  );
}

function PanelHead({
  id,
  kicker,
  title,
  aside,
}: {
  id: string;
  kicker: string;
  title: string;
  aside?: ReactNode;
}) {
  return (
    <div className="rv-home__panel-head">
      <div className="rv-home__panel-title">
        <span className="rv-home__kicker">{kicker}</span>
        <h2 id={id}>{title}</h2>
      </div>
      {aside}
    </div>
  );
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export default function Home() {
  // Pro supplies the dataset-export attention state; core has none.
  const Attention = pro.HomeAttention;
  if (!Attention) return <HomeView attention={NO_HOME_ATTENTION} />;
  return <Attention>{(attention) => <HomeView attention={attention} />}</Attention>;
}

function HomeView({ attention }: { attention: HomeAttentionState }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const onboarding = useOnboarding();

  // Same query key and options as `Layout.useObsHealthy`, so the two share one
  // cache entry: whether the "Open OBS" link below is worth showing.
  const obsHealthy = useObsHealthy();

  // These exported shell hooks keep Home's header copy on the exact same
  // deployment fact and cache entry as the navigation shell.
  const editionQ = useEditionQuery();
  const edition = editionQ.data?.edition ?? null;
  const isPro = useIsProEdition();

  const metricsQ = useQuery<MetricsOverview>({
    queryKey: ["metrics-overview"],
    queryFn: () => http<MetricsOverview>("/api/v1/metrics/overview"),
    refetchInterval: 30_000,
    retry: 1,
  });

  const robotsQ = useQuery<{ items: RobotListItem[] }>({
    queryKey: ["robots-list"],
    queryFn: () => http<{ items: RobotListItem[] }>("/api/v1/robots/list"),
    staleTime: 30_000,
  });

  const kpis = metricsQ.data?.kpis;
  // Every KPI comes from the same connection (`routers/obs.py`'s single
  // `psycopg.connect`), so all four null together means that connection
  // failed -- not that the fleet is quiet. A quiet fleet still reports
  // uploads_last_hour=0 etc, not null; null is reserved for "could not ask".
  const metricsDown =
    metricsQ.isError ||
    (!!metricsQ.data && Object.values(metricsQ.data.kpis).every((v) => v == null));

  const robots = robotsQ.data?.items ?? [];
  const fleetTotals = robots.reduce(
    (acc, r) => ({
      uploads: acc.uploads + r.total_uploads,
      completed: acc.completed + r.completed_uploads,
      bytes: acc.bytes + r.total_bytes,
      failed: acc.failed + r.failed_uploads,
      uploading: acc.uploading + r.uploading,
    }),
    { uploads: 0, completed: 0, bytes: 0, failed: 0, uploading: 0 }
  );
  const staleRobots = robots.filter((robot) => {
    if (!robot.last_upload_at) return true;
    const lastUpload = Date.parse(robot.last_upload_at);
    return !Number.isFinite(lastUpload) || Date.now() - lastUpload > 7 * 24 * 3600_000;
  });
  const hasFleetAttention =
    fleetTotals.failed > 0 || fleetTotals.uploading > 0 || staleRobots.length > 0;
  const attentionCount = robotsQ.data
    ? (fleetTotals.failed > 0 ? 1 : 0) +
      (fleetTotals.uploading > 0 ? 1 : 0) +
      (staleRobots.length > 0 ? 1 : 0) +
      attention.count
    : 0;
  const recentRobots = [...robots]
    .filter((robot) => robot.last_upload_at && Number.isFinite(Date.parse(robot.last_upload_at)))
    .sort((a, b) => Date.parse(b.last_upload_at || "") - Date.parse(a.last_upload_at || ""))
    .slice(0, 4);
  const staleNames = staleRobots.slice(0, 3).map((robot) => robot.display_name || robot.robot_id);

  const nowIso = new Date().toISOString();
  const oneHourAgoIso = new Date(Date.now() - 3600_000).toISOString();
  const dayAgoIso = new Date(Date.now() - 24 * 3600_000).toISOString();

  // The header's live indicator reports only what the metrics query itself
  // knows: loading, failed, refetching, or the wall-clock time of its last
  // answer. A clock time stays true between the 30s polls; "12s ago" would not.
  const live: { tone: ChipTone; text: string } = metricsQ.isLoading
    ? { tone: "neutral", text: "Loading live counts…" }
    : metricsDown
      ? { tone: "warn", text: "Live counts unavailable" }
      : metricsQ.isFetching
        ? { tone: "info", text: "Refreshing…" }
        : {
          tone: "ok",
          text: `Live · updated ${new Date(metricsQ.dataUpdatedAt).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}`,
        };

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ["metrics-overview"] });
    queryClient.invalidateQueries({ queryKey: ["robots-list"] });
    queryClient.invalidateQueries({ queryKey: ["robot-summary"] });
    queryClient.invalidateQueries({ queryKey: ["datasets"] });
  };

  const registryValue = (value: ReactNode) => (robotsQ.data ? value : "—");

  return (
    <div className="rv-home">
      <PageHeader
        kicker="Overview"
        title="Fleet overview"
        description={
          <>
            Where each recording is in the pipeline, and what needs an operator now.
            {isPro
              ? " Episodes and dataset cuts are available on this deployment."
              : edition
                ? " Episodes and dataset cuts are available in Ingotron Pro."
                : ""}
          </>
        }
        actions={
          <>
            <PageStatus tone={live.tone === "bad" ? "error" : live.tone}>{live.text}</PageStatus>
            <Button className="rv-home__btn" variant="secondary" onClick={refreshAll}>
              <RefreshCw size={16} aria-hidden="true" /> Refresh
            </Button>
            <Link className="rv-home__btn is-primary" to="/uploads">Open Intake</Link>
          </>
        }
      />

      <section className="rv-home__flow-section" aria-labelledby="rv-home-flow-title">
        <h2 id="rv-home-flow-title" className="rv-home__kicker">
          Pipeline flow · live counts from Postgres, refreshed every 30s
        </h2>

        <div className="rv-home__flow-frame">
          <div className="rv-home__flow">
            <FlowStage
              node="01"
              stage="Intake"
              label="Uploads · 1h"
              to={`/uploads?since=${encodeURIComponent(oneHourAgoIso)}&until=${encodeURIComponent(nowIso)}`}
              value={kpis?.uploads_last_hour}
              hint="completed or in flight, last 60 minutes"
              loading={metricsQ.isLoading}
              facts={
                <>
                  {robotsQ.data && fleetTotals.uploading > 0 ? (
                    <Chip tone="info" title="Uploads in flight now, across the registry">
                      {fleetTotals.uploading.toLocaleString()} in flight
                    </Chip>
                  ) : null}
                  {robotsQ.data && fleetTotals.failed > 0 ? (
                    <Chip tone="bad" title="Failed uploads across the registry, all time">
                      {fleetTotals.failed.toLocaleString()} failed
                    </Chip>
                  ) : null}
                  <Link
                    className="rv-home__link"
                    to={`/uploads?since=${encodeURIComponent(dayAgoIso)}&until=${encodeURIComponent(nowIso)}`}
                  >
                    Uploads · 24h
                  </Link>
                </>
              }
            />
            <FlowStage
              node={null}
              stage="Storage"
              label="Stored"
              value={kpis?.storage_bytes_total}
              format={formatBytes}
              hint="sum of completed upload bytes"
              loading={metricsQ.isLoading}
              facts={
                <>
                  {robotsQ.data ? (
                    <span className="rv-home__stage-fact">
                      {fleetTotals.completed.toLocaleString()} completed{" "}
                      {plural(fleetTotals.completed, "upload", "uploads")}
                    </span>
                  ) : null}
                  <Link className="rv-home__link" to="/explore">Explore</Link>
                </>
              }
            />
            <FlowStage
              node="03"
              stage="Review"
              label="Awaiting QA"
              to="/episodes"
              value={kpis?.episodes_awaiting_qa}
              hint="episodes with no QA report yet"
              loading={metricsQ.isLoading}
              emphasis="warn"
              facts={isPro ? undefined : <span className="rv-home__stage-fact">Available in Pro</span>}
            />
            <FlowStage
              node="04"
              stage="Verification"
              label="Exports · 24h"
              to="/datasets"
              value={kpis?.exports_completed_24h}
              hint="export jobs completed, last 24 hours"
              loading={metricsQ.isLoading}
              facts={
                attention.tileFact ??
                (isPro ? undefined : <span className="rv-home__stage-fact">Available in Pro</span>)
              }
            />
          </div>
        </div>

        {!metricsQ.isLoading && metricsDown && (
          <div className="rv-home__degraded">
            <span>
              Stats unavailable — the query service could not reach the database. The rest of this page is
              unaffected.
            </span>
            <span className="rv-home__degraded-actions">
              {obsHealthy && <Link className="rv-home__link" to="/obs">Open OBS</Link>}
              <Button className="rv-home__btn is-sm" size="sm" variant="secondary" onClick={() => metricsQ.refetch()}>
                Retry
              </Button>
            </span>
          </div>
        )}
      </section>

      <div className="rv-home__split">
        <section className="rv-home__panel" aria-labelledby="rv-home-attention-title">
          <PanelHead
            id="rv-home-attention-title"
            kicker="Review"
            title="Needs attention"
            aside={
              attentionCount > 0 ? (
                <Chip tone="warn">{attentionCount} {plural(attentionCount, "item", "items")}</Chip>
              ) : null
            }
          />

          {attention.notice ||
          robotsQ.isLoading ||
          robotsQ.isError ||
          attention.error ||
          (robotsQ.data && robots.length === 0) ||
          (robotsQ.data && robots.length > 0 && !hasFleetAttention && attention.count === 0 && !attention.unknown) ? (
            <div className="rv-home__panel-body">
              {attention.notice ? (
                <p className="rv-home__recovery-notice" role="status">{attention.notice}</p>
              ) : null}
              {robotsQ.isLoading ? <Skeleton className="h-12 w-full" /> : null}
              {robotsQ.isError ? (
                <p>
                  Fleet summary unavailable; upload failures and stale robots cannot be assessed right now. The Fleet
                  section below provides the retry control and error detail.
                </p>
              ) : null}
              {attention.error ? (
                <div className="rv-home__inline-error">
                  <span>Dataset export status is unavailable; recovery exceptions cannot be assessed right now.</span>
                  <Button className="rv-home__btn is-sm" size="sm" variant="secondary" onClick={attention.retry}>
                    Retry
                  </Button>
                </div>
              ) : null}
              {robotsQ.data && robots.length === 0 && attention.count === 0 ? (
                <p>Nothing to assess yet: upload failures and stale robots appear here once a robot is registered.</p>
              ) : null}
              {robotsQ.data && robots.length > 0 && !hasFleetAttention && attention.count === 0 && !attention.unknown ? (
                <p>
                  No failed or in-flight uploads are reported, and every registered robot uploaded within the last week.
                </p>
              ) : null}
            </div>
          ) : null}

          {robotsQ.data && (hasFleetAttention || attention.count > 0) ? (
            <ul className="rv-home__rows">
              {fleetTotals.failed > 0 ? (
                <AttentionRow
                  tone="bad"
                  label="Upload failed"
                  subject={`${fleetTotals.failed.toLocaleString()} failed ${plural(fleetTotals.failed, "upload needs", "uploads need")} review.`}
                  detail="Counted across every registered robot."
                  action={<Link className="rv-home__btn is-sm" to="/uploads?status=failed">Open failed uploads</Link>}
                />
              ) : null}
              {fleetTotals.uploading > 0 ? (
                <AttentionRow
                  tone="info"
                  label="In flight"
                  subject={`${fleetTotals.uploading.toLocaleString()} ${plural(fleetTotals.uploading, "upload is", "uploads are")} still in flight.`}
                  detail="Transfers the registry has not marked complete yet."
                  action={<Link className="rv-home__btn is-sm" to="/uploads?status=uploading">Open uploads</Link>}
                />
              ) : null}
              {staleRobots.length > 0 ? (
                <AttentionRow
                  tone="warn"
                  label="Stale"
                  subject={`${staleRobots.length.toLocaleString()} ${plural(staleRobots.length, "robot has", "robots have")} no upload in seven days.`}
                  detail={`${staleNames.join(", ")}${staleRobots.length > staleNames.length ? ` and ${staleRobots.length - staleNames.length} more` : ""}`}
                  action={<Link className="rv-home__btn is-sm" to="/uploads">Review uploads</Link>}
                />
              ) : null}
              {attention.rows}
            </ul>
          ) : null}
        </section>

        <section className="rv-home__panel" aria-labelledby="rv-home-ingestion-title">
          <PanelHead
            id="rv-home-ingestion-title"
            kicker="Live registry totals"
            title="Ingestion"
          />
          <div className="rv-home__facts-frame">
            <dl className="rv-home__facts">
              <div><dt>Registered</dt><dd>{registryValue(robots.length.toLocaleString())}</dd></div>
              <div>
                <dt>In flight</dt>
                <dd className={robotsQ.data && fleetTotals.uploading > 0 ? "is-info" : ""}>
                  {registryValue(fleetTotals.uploading.toLocaleString())}
                </dd>
              </div>
              <div>
                <dt>Failed</dt>
                <dd className={robotsQ.data && fleetTotals.failed > 0 ? "is-bad" : ""}>
                  {registryValue(fleetTotals.failed.toLocaleString())}
                </dd>
              </div>
              <div><dt>Fleet storage</dt><dd>{registryValue(formatBytes(fleetTotals.bytes))}</dd></div>
            </dl>
          </div>

          <div className="rv-home__subhead">
            <h3>Upload signals</h3>
            <Link className="rv-home__link" to="/uploads">View all</Link>
          </div>
          <div className="rv-home__signals">
            {robotsQ.isLoading ? <Skeleton className="h-20 w-full" /> : null}
            {robotsQ.isError ? (
              <div className="rv-home__inline-error">
                <span>Recent upload signals are unavailable because the robot registry did not answer.</span>
                <Button className="rv-home__btn is-sm" size="sm" variant="secondary" onClick={() => { void robotsQ.refetch(); }}>
                  Retry
                </Button>
              </div>
            ) : null}
            {robotsQ.data && robots.length === 0 ? (
              <p>No robots registered yet. Add one in Fleet or connect an upload agent.</p>
            ) : robotsQ.data && recentRobots.length === 0 ? (
              <p>No robot uploads have been recorded yet.</p>
            ) : null}
            {recentRobots.length > 0 ? (
              <ul>
                {recentRobots.map((robot) => {
                  const status = fleetNodeState(robot);
                  return (
                    <li key={robot.robot_id}>
                      <Link to={`/uploads?robot_id=${encodeURIComponent(robot.robot_id)}`}>
                        <span className="rv-home__signal-copy">
                          <b>{robot.display_name || robot.robot_id}</b>
                          <small>Last upload {relativeTime(robot.last_upload_at)}</small>
                        </span>
                        <Chip tone={STATE_TONE[status.state]}>{status.label}</Chip>
                        <span className="rv-home__signal-count">
                          {robot.total_uploads.toLocaleString()} {plural(robot.total_uploads, "upload", "uploads")}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </div>
        </section>
      </div>

      <section className="rv-home__panel" aria-labelledby="rv-home-fleet-title">
        <PanelHead
          id="rv-home-fleet-title"
          kicker={
            robotsQ.data
              ? `Registry · ${robots.length.toLocaleString()} ${plural(robots.length, "robot", "robots")}`
              : "Registry"
          }
          title="Fleet"
          aside={
            <span className="rv-home__panel-note">Select a robot for its recent uploads and processed topics.</span>
          }
        />
        {robotsQ.data && robots.length > 0 ? (
          <p className="rv-home__fleet-summary">
            {fleetTotals.uploads.toLocaleString()} {plural(fleetTotals.uploads, "upload", "uploads")} ·{" "}
            {formatBytes(fleetTotals.bytes)} stored
            {fleetTotals.uploading > 0 ? ` · ${fleetTotals.uploading} in flight` : ""}
            {fleetTotals.failed > 0 ? ` · ${fleetTotals.failed} failed` : ""}
          </p>
        ) : null}

        {robotsQ.isLoading && (
          <div className="rv-home__panel-body">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="mb-3 h-4" style={{ width: `${95 - i * 10}%` }} />
            ))}
          </div>
        )}

        {robotsQ.isError && (
          <div className="rv-home__panel-body rv-home__fleet-error">
            <strong>Could not load the fleet</strong>
            <p>
              <code>GET /api/v1/robots/list</code> did not answer. This reads the registry directly, so a
              failure here means the query service or its database is unreachable — not that the fleet is empty.
            </p>
            <Button className="rv-home__btn is-sm" size="sm" variant="secondary" onClick={() => robotsQ.refetch()}>
              Retry
            </Button>
          </div>
        )}

        {robotsQ.data && robots.length === 0 && (
          <div className="rv-home__panel-body">
            <EmptyState
              className="border-0"
              title="No robots registered"
              description="Add a robot in Fleet or connect an upload agent. The robot appears here after registration."
              action={
                <div className="rv-home__empty-actions">
                  <Link className="rv-home__btn is-primary" to="/robots?add=1">Add your first robot</Link>
                  <Button className="rv-home__btn" size="sm" variant="secondary" onClick={onboarding?.startTour} disabled={!onboarding}>
                    Start management tour
                  </Button>
                  <Button className="rv-home__btn" size="sm" variant="secondary" onClick={onboarding?.openHelp} disabled={!onboarding}>
                    Open task-oriented help
                  </Button>
                  <Link className="rv-home__btn" to="/admin">Check ingest activity</Link>
                </div>
              }
            />
          </div>
        )}

        {robotsQ.data && robots.length > 0 && (
          <div className="rv-home__table-scroll">
            <Table className="rv-home__table">
              <THead>
                <tr>
                  <TH scope="col">Robot</TH>
                  <TH scope="col">State</TH>
                  <TH scope="col">Last upload</TH>
                  <TH scope="col" className="rv-home__num">Uploads</TH>
                  <TH scope="col" className="rv-home__num">Completed</TH>
                  <TH scope="col" className="rv-home__num">Failed</TH>
                  <TH scope="col" className="rv-home__num">In flight</TH>
                  <TH scope="col" className="rv-home__num">Stored</TH>
                  <TH scope="col">Open</TH>
                </tr>
              </THead>
              <tbody>
                {robots.map((r) => (
                  <FleetRow
                    key={r.robot_id}
                    robot={r}
                    isPro={isPro}
                    expanded={expanded === r.robot_id}
                    onToggle={() => setExpanded((prev) => (prev === r.robot_id ? null : r.robot_id))}
                  />
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </section>
    </div>
  );
}
