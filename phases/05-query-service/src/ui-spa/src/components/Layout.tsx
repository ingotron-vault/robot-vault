import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  CircleHelp,
  Cpu,
  LayoutDashboard,
  Radio,
  Settings,
} from "lucide-react";
import {
  Button,
  HelpPanel,
  type HelpSection,
  Tour,
  type TourStep,
  useTour,
} from "@robot-vault/ui-kit";
import { http } from "../lib/api";
import { pro } from "../pro";
import AuthControl from "./AuthControl";
import { OnboardingProvider } from "./OnboardingContext";
import { OrbitThemeBoundary, OrbitThemeToggle } from "./OrbitTheme";
import { PostureChip } from "./PostureChip";
import "./Layout.css";

interface EditionResponse {
  edition: string;
  // Sent by newer servers (adapter identity); absent on older ones.
  core_version?: string;
}

// The public source repository.
export const SOURCE_REPO_URL = "https://github.com/ingotron-vault/robot-vault";
// Build-time fallback when the server does not report `core_version`.
const BUILD_VERSION: string | undefined = import.meta.env.VITE_RV_VERSION;

// AGPL-3.0 section 13: tell every user of the network service where the
// source is. The link points at the release tag of the running version.
export function SourceNotice({ version }: { version?: string }) {
  const href = version ? `${SOURCE_REPO_URL}/tree/v${version}` : SOURCE_REPO_URL;
  return (
    <p className="rv-shell__source" aria-label="Version and licence">
      {version ? <span>v{version}</span> : null}
      <span>AGPL-3.0</span>
      <a href={href} target="_blank" rel="noreferrer">Source</a>
    </p>
  );
}

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `rv-shell__nav-link${isActive ? " is-active" : ""}`;

// The core/pro edition split. The Pro pages exist only in the
// Pro UI build (`pro.installed`), and only a Pro backend answers their APIs.
// So a nav link to /ui/episodes or /ui/datasets is shown only when BOTH are
// true. In every other case those routes show the "Available in Pro" panel.
// The server is the gate; this is presentation only.
//
// While the edition is loading, or if the request fails, the Pro links stay
// hidden: the build and the server have not both confirmed Pro.
export function useEditionQuery() {
  return useQuery<EditionResponse>({
    queryKey: ["edition"],
    queryFn: () => http<EditionResponse>("/api/v1/edition"),
    // A deployment property: it cannot change without the container being
    // recreated, which reloads the SPA anyway.
    staleTime: Infinity,
    gcTime: Infinity,
    retry: 1,
  });
}

export function useIsProEdition(): boolean {
  const { data } = useEditionQuery();
  return data?.edition === "pro" && pro.installed;
}

interface ObsHealthResponse {
  status: { prometheus: { ok: boolean } | null };
}

// Prometheus + `/ui/obs` stay as an
// opt-in advanced tool behind the `obs` compose profile, which is down more
// often than up (it is not part of the default 10-service stack). Polling
// `/api/obs/health` -- itself lightweight, a 5s-timeout ping at the query
// service -- lets the nav link reflect that instead of always being there.
//
// Defaults to HIDING the link on error/timeout/loading. The `obs` profile is
// normally absent, so a dead link sitting in the nav would be the common case
// if this defaulted the other way.
export function useObsHealthy(): boolean {
  const { data, isError } = useQuery<ObsHealthResponse>({
    queryKey: ["obs-health"],
    queryFn: () => http<ObsHealthResponse>("/api/obs/health"),
    refetchInterval: 30_000,
    retry: false,
  });
  if (isError || !data) return false;
  return data.status.prometheus?.ok === true;
}

interface MetricsOverview {
  kpis: {
    uploads_last_hour: number | null;
    storage_bytes_total: number | null;
    episodes_awaiting_qa: number | null;
    exports_completed_24h: number | null;
  };
}

interface PipelineCount {
  text: string;
  description: string;
  hot?: boolean;
}

interface PipelineStep {
  key: string;
  id?: string;
  node: string;
  label: string;
  to: string;
  count: PipelineCount | null;
}

const countFormat = new Intl.NumberFormat("en-US");

function plural(value: number, one: string, many: string) {
  return value === 1 ? one : many;
}

// Live pipeline counts share Home's query (same key, endpoint, and 30s poll),
// so the shell adds no extra request while Overview is open. A null KPI means
// its source is unavailable: the count is left out rather than shown as 0.
function pipelineCount(
  value: number | null | undefined,
  build: (value: number, formatted: string) => PipelineCount
): PipelineCount | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return build(value, countFormat.format(value));
}

// The Tour card sits beside the sidebar on desktop and below the top bar on
// tablet and mobile, where the sidebar collapses (see Layout.css, 1120px).
function sidebarTourPlacement(): "right" | "bottom" {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "right";
  return window.matchMedia("(max-width: 1120px)").matches ? "bottom" : "right";
}

function LayoutContent() {
  const editionQ = useEditionQuery();
  const isPro = useIsProEdition();
  const obsHealthy = useObsHealthy();
  const [helpOpen, setHelpOpen] = useState(false);
  const [tourRequested, setTourRequested] = useState(false);
  const tour = useTour("viewer-management");
  const tenantQ = useQuery<{ scope: string }>({
    queryKey: ["tenant"],
    queryFn: () => http<{ scope: string }>("/api/tenant"),
    staleTime: Infinity,
    retry: 1,
  });
  const metricsQ = useQuery<MetricsOverview>({
    queryKey: ["metrics-overview"],
    queryFn: () => http<MetricsOverview>("/api/v1/metrics/overview"),
    refetchInterval: 30_000,
    retry: 1,
  });
  const kpis = metricsQ.isError ? undefined : metricsQ.data?.kpis;
  const pipeline: PipelineStep[] = [
    {
      key: "intake",
      id: "viewer-uploads",
      node: "01",
      label: "Intake",
      to: "/uploads",
      count: pipelineCount(kpis?.uploads_last_hour, (n, f) => ({
        text: `${f} / 1h`,
        description: `${f} ${plural(n, "upload", "uploads")} in the last hour`,
      })),
    },
    { key: "explore", node: "02", label: "Explore", to: "/explore", count: null },
    ...(isPro
      ? [
          {
            key: "review",
            id: "viewer-episodes",
            node: "03",
            label: "Review",
            to: "/episodes",
            count: pipelineCount(kpis?.episodes_awaiting_qa, (n, f) => ({
              text: `${f} open`,
              description: `${f} ${plural(n, "episode", "episodes")} awaiting review`,
              hot: n > 0,
            })),
          },
          {
            key: "verification",
            id: "viewer-datasets",
            node: "04",
            label: "Verification",
            to: "/datasets",
            count: pipelineCount(kpis?.exports_completed_24h, (n, f) => ({
              text: `${f} / 24h`,
              description: `${f} ${plural(n, "export", "exports")} completed in the last 24 hours`,
            })),
          },
        ]
      : []),
  ];
  const tourPlacement = sidebarTourPlacement();

  // On tablet and mobile the navigation is one horizontally scrolling row, so
  // the current page can sit off-screen (Admin, Observability). Bring it into
  // the row's view without scrolling the page. A no-op on desktop, where the
  // sidebar column does not scroll horizontally.
  const navsRef = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  useEffect(() => {
    const row = navsRef.current;
    if (!row || row.scrollWidth <= row.clientWidth) return;
    const active = row.querySelector<HTMLElement>(".is-active");
    if (!active) return;
    const rowBox = row.getBoundingClientRect();
    const box = active.getBoundingClientRect();
    if (box.left < rowBox.left || box.right > rowBox.right) {
      row.scrollLeft += box.left - rowBox.left - (rowBox.width - box.width) / 2;
    }
  }, [pathname, isPro, obsHealthy]);

  const tourSteps: TourStep[] = [
    {
      targetSelector: "#viewer-home",
      title: "Start with the fleet overview",
      body: "KPIs degrade independently when their data source is unavailable, so a quiet fleet is never confused with an outage.",
      placement: tourPlacement,
    },
    {
      targetSelector: "#viewer-uploads",
      title: "Review intake",
      body: "Intake has capture context, in-flight status, and drill-down details.",
      placement: tourPlacement,
    },
    ...(isPro
      ? [
          {
            targetSelector: "#viewer-episodes",
            title: "Assess review findings",
            body: "Review separates DEFECT from RISK and keeps PII handling visible.",
            placement: tourPlacement,
          },
          {
            targetSelector: "#viewer-datasets",
            title: "Verify dataset cuts",
            body: "Verification exposes export state, limitations, and the offline provenance command.",
            placement: tourPlacement,
          },
        ]
      : []),
    {
      targetSelector: "#viewer-help",
      title: "Reopen help any time",
      body: "This help panel can restart the tour and points to the offline user guide.",
      placement: tourPlacement,
    },
  ];
  const openHelp = useCallback(() => {
    setHelpOpen(true);
  }, []);
  const restartTour = useCallback(() => {
    setHelpOpen(false);
    setTourRequested(true);
    tour.start();
  }, [tour.start]);
  const onboardingActions = useMemo(
    () => ({ startTour: restartTour, openHelp }),
    [openHelp, restartTour]
  );
  const help: HelpSection[] = [
    {
      id: "overview",
      title: "Understand the overview",
      keywords: ["kpi", "fleet"],
      body: "Use the fleet table to open an individual robot, its intake, review findings (Pro), or Explore.",
    },
    ...(isPro
      ? [{
          id: "curation",
          title: "Cut and verify a dataset",
          keywords: ["dataset", "provenance", "export"],
          body: "Filter Review, create a cut, then use Verification to download and verify its signed manifest offline.",
        }]
      : []),
    {
      id: "core",
      title: "Edition behaviour",
      keywords: ["core", "pro"],
      body: "Review and Verification are absent in core edition because their server routers are not mounted. The navigation and tour follow that deployment fact.",
    },
    {
      id: "tour",
      title: "Run the tour again",
      keywords: ["onboarding"],
      body: <Button size="sm" variant="secondary" onClick={restartTour}>Start tour</Button>,
    },
    {
      id: "offline-guide",
      title: "Open the operator guide",
      keywords: ["guide", "offline", "runbook"],
      body: <>
        <p>{isPro
          ? "The local guide covers intake, review, dataset cuts, and offline verification."
          : "The local guide covers intake and stream exploration."}</p>
        <Link className="rv-shell__help-guide-link" to="/help" onClick={() => setHelpOpen(false)}>Open guide</Link>
      </>,
    },
  ];

  return (
    <div className="rv-shell">
      <a className="rv-shell__skip-link" href="#main-content">Skip to content</a>
      <OnboardingProvider value={onboardingActions}>
        {/*
          The frame is the two-column grid (sidebar + main). HelpPanel and Tour
          stay outside it so their overlay roots never become grid cells.
        */}
        <div className="rv-shell__frame">
          <header className="rv-shell__sidebar">
            <Link className="rv-shell__brand" to="/" aria-label="Robot Vault home">
              <span className="rv-shell__brand-mark"><Radio size={17} strokeWidth={1.8} aria-hidden="true" /></span>
              <span className="rv-shell__brand-copy">
                <span><strong>Robot Vault</strong><em>Orbit</em></span>
                <small>Fleet data plane</small>
              </span>
            </Link>

            <div className="rv-shell__navs" ref={navsRef}>
              <nav aria-label="Primary navigation" className="rv-shell__nav">
                <ul className="rv-shell__nav-list">
                  <li>
                    <NavLink id="viewer-home" end to="/" className={navLinkClass}>
                      <LayoutDashboard size={16} aria-hidden="true" />
                      <span className="rv-shell__nav-label">Overview</span>
                    </NavLink>
                  </li>
                </ul>
                <div className="rv-shell__group">
                  <p className="rv-shell__kicker" id="rv-shell-pipeline-label">Pipeline</p>
                  <ol className="rv-shell__nav-list rv-shell__track" aria-labelledby="rv-shell-pipeline-label">
                    {pipeline.map((step) => (
                      <li key={step.to}>
                        <NavLink
                          id={step.id}
                          to={step.to}
                          className={navLinkClass}
                          aria-describedby={step.count ? `rv-shell-count-${step.key}` : undefined}
                        >
                          <span className="rv-shell__node" aria-hidden="true">{step.node}</span>
                          <span className="rv-shell__nav-label">{step.label}</span>
                          {step.count ? (
                            <>
                              <span
                                className={`rv-shell__count${step.count.hot ? " is-hot" : ""}`}
                                aria-hidden="true"
                              >
                                {step.count.text}
                              </span>
                              <span id={`rv-shell-count-${step.key}`} hidden>{step.count.description}</span>
                            </>
                          ) : null}
                        </NavLink>
                      </li>
                    ))}
                  </ol>
                </div>
              </nav>

              <nav className="rv-shell__group rv-shell__system-nav" aria-labelledby="rv-shell-system-label">
                <p className="rv-shell__kicker" id="rv-shell-system-label">System</p>
                <ul className="rv-shell__nav-list">
                  {/*
                    Shown to everyone, like /ui/admin beside it. The server is the
                    role gate (`require_role("admin")` on the provisioning proxy);
                    the page degrades to a read-only fleet view for a viewer rather
                    than 404-ing, so hiding the link would only cost an operator the
                    sentence that explains what they are missing.
                  */}
                  <li>
                    <NavLink to="/robots" className={navLinkClass}>
                      <Cpu size={16} aria-hidden="true" />
                      <span className="rv-shell__nav-label">Robots</span>
                    </NavLink>
                  </li>
                  <li>
                    {/* Visible "Admin" is contained in the accessible name (WCAG 2.5.3). */}
                    <NavLink to="/admin" className={navLinkClass} aria-label="Administration">
                      <Settings size={16} aria-hidden="true" />
                      <span className="rv-shell__nav-label">Admin</span>
                    </NavLink>
                  </li>
                  {obsHealthy ? (
                    <li>
                      <NavLink to="/obs" className={navLinkClass}>
                        <Activity size={16} aria-hidden="true" />
                        <span className="rv-shell__nav-label">Observability</span>
                      </NavLink>
                    </li>
                  ) : null}
                  <li>
                    {/* Help opens the in-app panel; the full guide is a link inside it. */}
                    <button
                      id="viewer-help"
                      type="button"
                      className="rv-shell__nav-link rv-shell__help"
                      aria-label="Open help"
                      onClick={openHelp}
                    >
                      <CircleHelp size={16} aria-hidden="true" />
                      <span className="rv-shell__nav-label">Help</span>
                    </button>
                  </li>
                </ul>
              </nav>
            </div>

            <div className="rv-shell__footer">
              <div className="rv-shell__posture" aria-label="Deployment context">
                {editionQ.isLoading ? (
                  <span className="rv-shell__posture-loading" aria-label="Loading edition" />
                ) : editionQ.data?.edition ? (
                  <PostureChip
                    tone={isPro ? "info" : "neutral"}
                    title="GET /api/v1/edition — fixed for the life of this container"
                  >
                    {editionQ.data.edition} edition
                  </PostureChip>
                ) : (
                  <PostureChip title="GET /api/v1/edition did not answer">edition unknown</PostureChip>
                )}
                {tenantQ.data?.scope ? (
                  <PostureChip mono title="Lake prefix scope this vault is bound to (GET /api/tenant)">
                    scope {tenantQ.data.scope}
                  </PostureChip>
                ) : null}
              </div>
              <div className="rv-shell__account">
                <AuthControl />
                <OrbitThemeToggle />
              </div>
              <SourceNotice version={editionQ.data?.core_version ?? BUILD_VERSION} />
            </div>
          </header>
          <main id="main-content" className="rv-shell__main" tabIndex={-1}>
            <Outlet />
          </main>
        </div>
        <HelpPanel
          open={helpOpen}
          onClose={() => setHelpOpen(false)}
          title="Robot Vault help"
          sections={help}
        />
        <Tour
          tourId="viewer-management"
          steps={tourSteps}
          open={tourRequested && tour.isOpen && !editionQ.isLoading}
          onClose={() => {
            setTourRequested(false);
            tour.close();
          }}
          onComplete={() => {
            setTourRequested(false);
            tour.complete();
          }}
        />
      </OnboardingProvider>
    </div>
  );
}

export default function Layout() {
  return (
    <OrbitThemeBoundary>
      <LayoutContent />
    </OrbitThemeBoundary>
  );
}
