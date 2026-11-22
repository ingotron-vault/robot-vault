import { ReactNode, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { cn } from "../cn";
import { useDialogFocus } from "./useDialogFocus";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Modal({ open, onClose, title, children, className }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useDialogFocus(open, panelRef, onClose);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="rv-overlay-backdrop absolute inset-0 bg-black/45" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-label={title ? undefined : "Dialog"}
        tabIndex={-1}
        className={cn(
          "relative w-full max-w-md rounded-lg border border-border bg-background p-4 shadow-lg",
          className
        )}
      >
        {title ? <div id={titleId} className="mb-4 text-sm font-semibold">{title}</div> : null}
        {children}
      </div>
    </div>,
    document.body,
  );
}
