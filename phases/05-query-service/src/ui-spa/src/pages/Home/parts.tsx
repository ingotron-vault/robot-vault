import type { ReactNode } from "react";
import { cn } from "@robot-vault/ui-kit";

// Pieces of the Home page that the Pro Home attention rows also use.

export type ChipTone = "ok" | "warn" | "bad" | "info" | "neutral";

/** Tinted status chip: the dot and the label both carry the tone, so colour is never the only signal. */
export function Chip({ tone, children, title }: { tone: ChipTone; children: ReactNode; title?: string }) {
  return (
    <span className={`rv-home__chip is-${tone}`} title={title}>
      <i aria-hidden="true" />
      {children}
    </span>
  );
}

/** One "Needs attention" row: status chip · subject and detail · action. */
export function AttentionRow({
  tone,
  label,
  subject,
  detail,
  action,
  nested = false,
  children,
}: {
  tone: ChipTone;
  label: string;
  subject: ReactNode;
  detail?: ReactNode;
  action?: ReactNode;
  nested?: boolean;
  children?: ReactNode;
}) {
  return (
    <li className={cn("rv-home__row", nested && "is-nested")}>
      <Chip tone={tone}>{label}</Chip>
      <div className="rv-home__row-copy">
        <span className="rv-home__row-subject">{subject}</span>
        {detail ? <span className="rv-home__row-detail">{detail}</span> : null}
      </div>
      {action ? <div className="rv-home__row-action">{action}</div> : null}
      {children}
    </li>
  );
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  const seconds = (Date.now() - t) / 1000;
  if (seconds < 0) return "just now";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

