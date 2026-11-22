import { createContext, ReactNode, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BASE_ROOT, Job, Robot, StatusResponse, discoverRobots, getJobs, getStatus } from "../lib/api";

const LAST_ROBOT_KEY = "lastRobot";

export function rootForRobotName(name: string): string {
  return name ? `${BASE_ROOT}/${name}` : BASE_ROOT;
}

function readInitialRoot(): string {
  try {
    return localStorage.getItem(LAST_ROBOT_KEY) || "";
  } catch {
    return "";
  }
}

// GET /api/upload/jobs never returns a `root` (JobStore.create_job only ever
// sees a flat file list -- see api.ts's Job interface), so "does this job
// belong to the selected robot" has to be inferred from the one thing a job
// DOES carry: its files' absolute paths. Every file in one job was uploaded
// from the same queue root, so a path-prefix match is exact in the common
// case (files directly under root) and still correct when the root was
// scanned recursively (files under a subdirectory of root).
function jobMatchesRoot(job: Job, root: string): boolean {
  if (!root) return false;
  const prefix = root.endsWith("/") ? root : `${root}/`;
  return job.files.some((f) => f.path === root || f.path.startsWith(prefix));
}

// Best-effort root for the reattach-on-mount case below, where there is no
// selected root yet to match against. Takes the directory of the job's first
// file -- usually exactly the queue root, but can be a subdirectory of it if
// that upload was started against a recursively-scanned subpath. Only ever
// used to pre-fill the root selector, never displayed as a fact.
function guessJobRoot(job: Job): string | undefined {
  const first = job.files[0]?.path;
  if (!first) return undefined;
  const idx = first.lastIndexOf("/");
  return idx > 0 ? first.slice(0, idx) : undefined;
}

interface RobotContextValue {
  root: string;
  setRoot: (root: string) => void;
  robots: Robot[];
  robotsLoading: boolean;
  robotsError: boolean;
  refetchRobots: () => void;
  status: StatusResponse | undefined;
  statusLoading: boolean;
  statusStale: boolean;
  activeJobId: string | undefined;
  activeJob: Job | undefined;
}

const RobotContext = createContext<RobotContextValue | null>(null);

export function RobotProvider({ children }: { children: ReactNode }) {
  const [root, setRootState] = useState<string>(readInitialRoot);
  const reattachedRef = useRef(false);
  // Mirrors `root` for synchronous reads inside the reattach effect below --
  // the effect's own closure over `root` would otherwise be frozen at the
  // value from mount (deps are intentionally `[]` so it runs exactly once),
  // which could miss a selection the user makes while the getJobs() request
  // is still in flight.
  const rootRef = useRef(root);

  const setRoot = (next: string) => {
    rootRef.current = next;
    setRootState(next);
    try {
      localStorage.setItem(LAST_ROBOT_KEY, next);
    } catch {
      /* ignore quota/private-mode errors */
    }
  };

  // Reload mid-upload: on first mount, check for a still-running job and, if
  // one exists, restore its (best-guess) root so the active-job card +
  // progress polling reattach automatically instead of requiring the user to
  // re-select the robot by hand.
  //
  // This must never clobber an *explicit* selection though -- if the user
  // (or localStorage's `lastRobot`) already has a root chosen, that choice
  // has to survive reload even when some other robot happens to have a job
  // running. So only auto-attach when nothing is currently selected; if a
  // root IS already selected, the root-scoped `activeJob` below (matched by
  // file path, see jobMatchesRoot) picks up a running job on *this* root on
  // its own, without switching roots.
  useEffect(() => {
    if (reattachedRef.current) return;
    reattachedRef.current = true;
    getJobs()
      .then((jobs) => {
        const running = jobs.find((j) => j.status === "running");
        const guessedRoot = running && guessJobRoot(running);
        if (guessedRoot && !rootRef.current) setRoot(guessedRoot);
      })
      .catch(() => {
        /* best-effort reattach only */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const robotsQuery = useQuery({
    queryKey: ["robots"],
    queryFn: discoverRobots,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const statusQuery = useQuery({
    queryKey: ["status", root],
    queryFn: () => getStatus(root),
    enabled: !!root,
    refetchInterval: 5000,
    staleTime: 0,
    gcTime: 5 * 60_000,
  });

  // The live view: GET /api/upload/jobs is process-global (not root-scoped),
  // so this is fetched once and filtered client-side per consumer. Poll
  // faster while a job for the SELECTED root is running, same as the old
  // /api/overview poll did.
  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: getJobs,
    refetchInterval: (query) => {
      const jobs = query.state.data ?? [];
      const runningForRoot = jobs.some((j) => j.status === "running" && jobMatchesRoot(j, root));
      return runningForRoot ? 2000 : 5000;
    },
  });

  const activeJob = root ? jobsQuery.data?.find((j) => j.status === "running" && jobMatchesRoot(j, root)) : undefined;
  const activeJobId = activeJob?.job_id;

  const value = useMemo<RobotContextValue>(
    () => ({
      root,
      setRoot,
      robots: robotsQuery.data?.robots ?? [],
      robotsLoading: robotsQuery.isLoading,
      robotsError: robotsQuery.isError,
      refetchRobots: () => robotsQuery.refetch(),
      status: statusQuery.data,
      statusLoading: statusQuery.isLoading,
      statusStale: statusQuery.isError,
      activeJobId,
      activeJob,
    }),
    [root, robotsQuery.data, robotsQuery.isLoading, robotsQuery.isError, statusQuery.data, statusQuery.isLoading, statusQuery.isError, activeJobId, activeJob]
  );

  return <RobotContext.Provider value={value}>{children}</RobotContext.Provider>;
}

export function useRobot(): RobotContextValue {
  const ctx = useContext(RobotContext);
  if (!ctx) throw new Error("useRobot must be used within a RobotProvider");
  return ctx;
}
