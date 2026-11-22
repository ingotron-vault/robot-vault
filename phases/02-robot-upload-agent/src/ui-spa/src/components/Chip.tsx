import { ReactNode } from "react";
import { cn, Tone } from "@robot-vault/ui-kit";

// Kit gap: StatusPill's displayed text is always the literal `status` value
// it maps through toneFor (pending/uploading/running/uploaded/completed/
// failed/cancelled/interrupted) -- there is no kit primitive for a
// custom-label pill in a specific semantic tone (e.g. "server reachable" /
// "unreachable", a connection-quality chip). Rather than hand-roll arbitrary
// colors OR misuse StatusPill with a mismatched status string just to get a
// color, this mirrors StatusPill's own tone -> class table (same design
// tokens: success/warning/danger/info/neutral) so every colored surface in
// the app still traces back to the shared Tone taxonomy. Candidate for
// promoting into the kit as a generic `Badge`/`Chip(tone, children)`.
const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-muted-foreground/10 text-muted-foreground",
  info: "bg-accent/10 text-accent",
  success: "bg-success/10 text-success",
  danger: "bg-danger/10 text-danger",
  warning: "bg-warning/10 text-warning",
};

export interface ChipProps {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  title?: string;
}

export function Chip({ tone = "neutral", children, className, title }: ChipProps) {
  return (
    <span
      title={title}
      className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", TONE_CLASSES[tone], className)}
    >
      {children}
    </span>
  );
}
