import type { ReactNode } from "react";
import { cn, toneClasses } from "@robot-vault/ui-kit";

/**
 * Deployment posture is not part of a backend status vocabulary: edition and
 * lake scope are server configuration facts, so they need their own explicit
 * labels while sharing the kit's accessible chip treatment. `mono` marks a
 * machine value such as the lake scope prefix.
 */
export function PostureChip({
  tone = "neutral",
  mono = false,
  children,
  title,
}: {
  tone?: "neutral" | "info";
  mono?: boolean;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "rv-posture-chip inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium",
        toneClasses(tone, "chip").wrapper,
        mono && "is-mono"
      )}
    >
      {children}
    </span>
  );
}
