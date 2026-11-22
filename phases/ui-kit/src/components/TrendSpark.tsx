import { useId } from "react";
import { cn } from "../cn";
import { Tone } from "../status";

export interface TrendSparkProps {
  values: number[];
  label?: string;
  tone?: Tone;
  className?: string;
}

const STROKE_CLASS: Record<Tone, string> = {
  neutral: "text-muted-foreground",
  info: "text-accent",
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
};

export function TrendSpark({ values, label = "Trend", tone = "info", className }: TrendSparkProps) {
  const titleId = useId();
  const usable = values.filter(Number.isFinite);
  if (usable.length < 2) return <span className={cn("text-xs text-muted-foreground", className)}>No trend</span>;
  const min = Math.min(...usable);
  const max = Math.max(...usable);
  const range = max - min || 1;
  const points = usable.map((value, index) => {
    const x = (index / (usable.length - 1)) * 100;
    const y = 28 - ((value - min) / range) * 24;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return (
    <svg viewBox="0 0 100 32" role="img" aria-labelledby={titleId} className={cn("h-8 w-24", className)}>
      <title id={titleId}>{label}</title>
      <polyline fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" points={points} className={STROKE_CLASS[tone]} />
    </svg>
  );
}
