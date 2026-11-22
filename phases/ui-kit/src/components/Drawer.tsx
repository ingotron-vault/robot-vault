import { ReactNode, useId, useRef } from "react";
import { cn } from "../cn";
import { useDialogFocus } from "./useDialogFocus";

/**
 * How wide the sheet is allowed to get.
 *
 * `md` (28rem) is the default. The wider steps exist because wide tables in
 * a 28rem sheet mean nested horizontal scrollers. A drawer that can be told
 * how much room its content needs is the knob, not a different component.
 *
 * Literal classes, not `max-w-${size}`: Tailwind's JIT scans source text.
 */
export type DrawerSize = "md" | "lg" | "xl";

const SIZE_CLASS: Record<DrawerSize, string> = {
  md: "max-w-md",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
};

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  /** Sheet width cap. Defaults to `md`. */
  size?: DrawerSize;
  className?: string;
}

export function Drawer({ open, onClose, title, children, size = "md", className }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useDialogFocus(open, panelRef, onClose);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="rv-overlay-backdrop absolute inset-0 bg-black/45" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-label={title ? undefined : "Details"}
        tabIndex={-1}
        className={cn(
          "relative h-full w-full overflow-y-auto border-l border-border bg-background p-4 shadow-lg",
          SIZE_CLASS[size] ?? SIZE_CLASS.md,
          className
        )}
      >
        <div className="mb-4 flex items-center gap-3">
          {title ? <div id={titleId} className="text-sm font-semibold">{title}</div> : null}
          <button
            type="button"
            className="ml-auto rounded-md border border-border px-2 py-1 text-sm"
            aria-label="Close details"
            onClick={onClose}
          >
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
