import { cn } from "../cn";
import { Tone } from "../status";
import { toneClasses } from "../toneClasses";

export interface ProgressBarProps {
  value: number;
  tone?: Tone;
  className?: string;
  /** Human-readable progress name; callers should make it task-specific. */
  label?: string;
}

export function ProgressBar({ value, tone = "info", className, label }: ProgressBarProps) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      role="progressbar"
      aria-label={label ?? "Progress"}
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-border", className)}
    >
      {/* The bar is the tone at full saturation -- the same `fill` the pills
          put on their dot, which is why it comes from the shared table rather
          than a fourth private copy of `bg-success` / `bg-warning` / ... */}
      <div
        className={cn("h-full rounded-full transition-all", toneClasses(tone).fill)}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
