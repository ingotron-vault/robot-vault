import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from "react";

export interface ActivityLogEntry {
  id: number;
  ts: number;
  message: string;
}

interface ActivityLogValue {
  entries: ActivityLogEntry[];
  log: (message: string) => void;
  clear: () => void;
}

const ActivityLogContext = createContext<ActivityLogValue | null>(null);

const MAX_ENTRIES = 200;

// Client-side-only activity log, ported from legacy static/app.js's #log
// panel: every view can append a line via useActivityLog().log(...), and
// the Settings & Diagnostics view renders the accumulated list. Nothing
// here is persisted or sent to the server.
export function ActivityLogProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<ActivityLogEntry[]>([]);

  const log = useCallback((message: string) => {
    setEntries((prev) => {
      const next = [...prev, { id: Date.now() + Math.random(), ts: Date.now(), message }];
      return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next;
    });
  }, []);

  const clear = useCallback(() => setEntries([]), []);

  const value = useMemo(() => ({ entries, log, clear }), [entries, log, clear]);

  return <ActivityLogContext.Provider value={value}>{children}</ActivityLogContext.Provider>;
}

export function useActivityLog(): ActivityLogValue {
  const ctx = useContext(ActivityLogContext);
  if (!ctx) throw new Error("useActivityLog must be used within an ActivityLogProvider");
  return ctx;
}
