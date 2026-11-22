import { ReactNode } from "react";
import { Stat } from "./Stat";
import { TrendSpark } from "./TrendSpark";

export interface KpiTileProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  trend?: number[];
  trendLabel?: string;
  className?: string;
}

export function KpiTile({ label, value, hint, trend, trendLabel, className }: KpiTileProps) {
  return (
    <section aria-label={label} className={className}>
      <Stat
        label={label}
        value={<span className="tabular-nums">{value}</span>}
        hint={hint}
        aside={trend ? <TrendSpark values={trend} label={trendLabel || `${label} trend`} /> : undefined}
      />
    </section>
  );
}
