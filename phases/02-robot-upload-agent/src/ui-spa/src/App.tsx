import { useState } from "react";
import { Button, HelpPanel, PageShell, ThemeToggle, Tour, useTour } from "@robot-vault/ui-kit";
import { ActivityLogProvider } from "./lib/activityLog";
import { RobotProvider } from "./context/RobotContext";
import { Route, useHashRoute } from "./lib/hashRoute";
import { StatusStrip } from "./components/StatusStrip";
import { Banners } from "./components/Banners";
import Dashboard from "./views/Dashboard";
import Files from "./views/Files";
import History from "./views/History";
import Settings from "./views/Settings";
import { QueueTray } from "./components/QueueTray";

const TABS: { route: Route; label: string }[] = [
  { route: "/", label: "Dashboard" },
  { route: "/files", label: "Files" },
  { route: "/history", label: "History" },
  { route: "/settings", label: "Settings" },
];

function NavLinks({ current }: { current: Route }) {
  return (
    <>
      {TABS.map((tab) => (
        <a
          key={tab.route}
          id={tab.route === "/files" ? "files-tab" : tab.route === "/history" ? "history-tab" : undefined}
          href={`#${tab.route}`}
          className={current === tab.route ? "font-medium text-accent" : "text-muted-foreground hover:underline"}
        >
          {tab.label}
        </a>
      ))}
    </>
  );
}

function ActiveView({ route }: { route: Route }) {
  switch (route) {
    case "/files":
      return <Files />;
    case "/history":
      return <History />;
    case "/settings":
      return <Settings />;
    case "/":
    default:
      return <Dashboard />;
  }
}

export default function App() {
  const route = useHashRoute();
  const tour = useTour("robot-upload-console");
  const [helpOpen, setHelpOpen] = useState(false);
  const help = [
    {
      id: "prepare",
      title: "Put recordings where the agent can see them",
      keywords: ["base root", "mount"],
      body: (
        <>
          The agent scans <code>BASE_ROOT</code>. For Docker, point <code>ROBOT_DATA_HOST_DIR</code> at the host
          directory before starting the robot UI.
        </>
      ),
    },
    {
      id: "upload",
      title: "Upload a selected batch",
      keywords: ["files", "queue"],
      body: "Choose MCAP files in Files, then start a batch. The queue tray reports per-file byte progress and any active-job API failure reason.",
    },
    {
      id: "recover",
      title: "Recover after an interruption",
      keywords: ["retry", "resume", "failed"],
      body: "Use Upload next batch for pending work or Retry failed for failed files. Large-file retries reuse parts the server has saved while its session remains available. Expired sessions and small uploads resend the whole file.",
    },
    {
      id: "history",
      title: "Confirm what happened",
      keywords: ["manifest", "history"],
      body: "History is built from durable per-file manifests, so it remains available after the agent restarts.",
    },
    {
      id: "tour",
      title: "Take the console tour again",
      keywords: ["onboarding", "start"],
      body: (
        <Button
          size="sm"
          variant="secondary"
          onClick={() => {
            setHelpOpen(false);
            tour.start();
          }}
        >
          Start tour
        </Button>
      ),
    },
  ];
  const tourSteps = [
    {
      targetSelector: "#robot-status",
      title: "This is your local operator console",
      body: "It runs next to the robot and only reports what the local upload agent can verify.",
      placement: "bottom" as const,
    },
    {
      targetSelector: "#robot-discovery",
      title: "Choose a discovered robot",
      body: "Each card shows the files discovered below the configured data root.",
      placement: "bottom" as const,
    },
    {
      targetSelector: "#files-tab",
      title: "Select files to upload",
      body: "Filter and select MCAP recordings, then start a batch when the ingest server is reachable.",
      placement: "bottom" as const,
    },
    {
      targetSelector: "#queue-tray",
      title: "Keep an eye on the queue",
      body: "The queue reports live progress and per-file failure reasons while a job runs. Retained jobs can be expanded in History after completion.",
      placement: "top" as const,
    },
    {
      targetSelector: "#history-tab",
      title: "Verify durable history",
      body: "History is reconstructed from local manifests and survives an agent restart.",
      placement: "bottom" as const,
    },
    {
      targetSelector: "#help-button",
      title: "Get task-oriented help",
      body: "Use this button any time to re-open help or relaunch this tour.",
      placement: "bottom" as const,
    },
  ];

  return (
    <RobotProvider>
      <ActivityLogProvider>
        <div className="robot-console flex min-h-screen flex-col">
        <PageShell
          title="Robot Upload Agent"
          className="robot-console-shell flex-1"
          nav={<NavLinks current={route} />}
          actions={
            <>
              <Button
                id="help-button"
                variant="ghost"
                size="sm"
                aria-label="Open help"
                onClick={() => setHelpOpen(true)}
              >
                ?
              </Button>
              <ThemeToggle />
            </>
          }
        >
          <StatusStrip />
          <Banners />
          <ActiveView route={route} />
        </PageShell>
        <QueueTray />
        </div>
        <HelpPanel open={helpOpen} onClose={() => setHelpOpen(false)} sections={help} />
        <Tour tourId="robot-upload-console" steps={tourSteps} open={tour.isOpen} onClose={tour.close} onComplete={tour.complete} />
      </ActivityLogProvider>
    </RobotProvider>
  );
}
