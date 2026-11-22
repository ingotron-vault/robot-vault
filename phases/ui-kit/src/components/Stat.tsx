import { ReactNode } from "react";
import { cn } from "../cn";

export interface StatProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  /** Optional visual placed beside the value, e.g. a compact trend sparkline. */
  aside?: ReactNode;
  className?: string;
}

export function Stat({ label, value, hint, aside, className }: StatProps) {
  return (
    <div className={cn("rounded-lg border border-border bg-card px-4 py-3", className)}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs text-muted-foreground">{label}</div>
          <div className="mt-1 text-2xl font-semibold">{value}</div>
        </div>
        {aside}
      </div>
      {hint ? <div className="mt-1 text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}
