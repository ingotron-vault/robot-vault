import { cn } from "../cn";
import { toneFor, ToneDomain } from "../status";
import { toneClasses } from "../toneClasses";

// Tone lives on the dot + a saturated border, with the label carried by the
// tone's own `-foreground` token (or plain `text-foreground` for tones that
// don't have one) -- the `hued` variant in ../toneClasses.ts, which holds the
// class table. Putting the tone on the label text
// directly, over its own 10% tint, measures ~1.8:1 for `warning` in light
// mode -- see phases/ui-kit/src/tokens.css's header comment.

export interface StatusPillProps {
  status: string;
  /**
   * Which status vocabulary `status` belongs to. Defaults to `upload`. With a
   * domain, `<StatusPill status="exporting" domain="export" />` is info rather
   * than an undifferentiated grey.
   */
  domain?: ToneDomain;
  className?: string;
}

export function StatusPill({ status, domain = "upload", className }: StatusPillProps) {
  const styles = toneClasses(toneFor(domain, status), "hued");
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        styles.wrapper,
        className
      )}
    >
      <span aria-hidden="true" className={cn("h-1.5 w-1.5 shrink-0 rounded-full", styles.fill)} />
      {status}
    </span>
  );
}
