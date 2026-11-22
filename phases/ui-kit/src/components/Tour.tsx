import {
  CSSProperties,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Button } from "./Button";
import { useDialogFocus } from "./useDialogFocus";

export type TourPlacement = "top" | "right" | "bottom" | "left";

export interface TourStep {
  targetSelector: string;
  title: string;
  body: string;
  placement?: TourPlacement;
}

export interface TourProps {
  tourId: string;
  steps: TourStep[];
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

export interface UseTourResult {
  isOpen: boolean;
  isCompleted: boolean;
  start: () => void;
  close: () => void;
  skip: () => void;
  complete: () => void;
  reset: () => void;
}

const keyFor = (tourId: string) => `robot-vault:tour:${tourId}:completed`;

function persisted(tourId: string): boolean {
  try { return localStorage.getItem(keyFor(tourId)) === "true"; } catch { return false; }
}

/** Local-first tour state. `start` deliberately reopens a completed tour. */
export function useTour(tourId: string): UseTourResult {
  const [isCompleted, setCompleted] = useState(() => persisted(tourId));
  const [isOpen, setOpen] = useState(() => !persisted(tourId));
  useEffect(() => {
    const done = persisted(tourId);
    setCompleted(done);
    setOpen(!done);
  }, [tourId]);
  const mark = useCallback(() => {
    try { localStorage.setItem(keyFor(tourId), "true"); } catch { /* private mode */ }
    setCompleted(true);
    setOpen(false);
  }, [tourId]);
  const start = useCallback(() => setOpen(true), []);
  const close = useCallback(() => setOpen(false), []);
  const reset = useCallback(() => {
    try { localStorage.removeItem(keyFor(tourId)); } catch { /* ignore */ }
    setCompleted(false);
    setOpen(true);
  }, [tourId]);
  return useMemo(
    () => ({
      isOpen,
      isCompleted,
      start,
      close,
      skip: mark,
      complete: mark,
      reset,
    }),
    [close, isCompleted, isOpen, mark, reset, start]
  );
}

function cardStyle(rect: DOMRect | null, placement: TourPlacement): CSSProperties {
  if (!rect) return { left: "50%", top: "50%", transform: "translate(-50%, -50%)" };
  const gap = 14;
  if (placement === "top") return { left: Math.max(12, rect.left), bottom: window.innerHeight - rect.top + gap };
  if (placement === "left") return { top: Math.max(12, rect.top), right: window.innerWidth - rect.left + gap };
  if (placement === "right") return { top: Math.max(12, rect.top), left: rect.right + gap };
  return { left: Math.max(12, rect.left), top: rect.bottom + gap };
}

export function Tour({ tourId, steps, open, onClose, onComplete }: TourProps) {
  const [index, setIndex] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const titleId = useId();
  const bodyId = useId();
  const current = steps[index];
  // Esc has the same completion semantics as Skip: an operator who actively
  // dismisses first-run guidance should not see it on every page load.
  useDialogFocus(open, panelRef, onComplete);

  const locate = useCallback(() => {
    const target = current ? document.querySelector(current.targetSelector) as HTMLElement | null : null;
    setRect(target?.getBoundingClientRect() ?? null);
    return Boolean(target);
  }, [current]);

  const nextAvailable = useCallback((from: number, direction: 1 | -1): number | null => {
    for (let candidate = from + direction; candidate >= 0 && candidate < steps.length; candidate += direction) {
      if (document.querySelector(steps[candidate].targetSelector)) return candidate;
    }
    return null;
  }, [steps]);

  useLayoutEffect(() => {
    if (!open) return;
    if (!locate()) {
      const next = nextAvailable(index, 1) ?? nextAvailable(index, -1);
      if (next == null) onComplete();
      else setIndex(next);
    }
  }, [index, locate, nextAvailable, onComplete, open]);

  useEffect(() => {
    if (!open) return;
    const refresh = () => {
      if (frameRef.current != null) return;
      frameRef.current = window.requestAnimationFrame(() => {
        frameRef.current = null;
        locate();
      });
    };
    window.addEventListener("resize", refresh);
    window.addEventListener("scroll", refresh, true);
    return () => {
      window.removeEventListener("resize", refresh);
      window.removeEventListener("scroll", refresh, true);
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [locate, open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowRight") { event.preventDefault(); const next = nextAvailable(index, 1); if (next != null) setIndex(next); }
      if (event.key === "ArrowLeft") { event.preventDefault(); const previous = nextAvailable(index, -1); if (previous != null) setIndex(previous); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [index, nextAvailable, open]);

  useEffect(() => {
    if (open) setIndex(0);
  }, [open, tourId]);

  // Do not query the DOM while rendering: on a first render the targets may
  // not be mounted yet, which would falsely turn the first Next button into
  // Done. Missing later targets are resolved in the click handler instead.
  const isLast = index === steps.length - 1;
  if (!open || !current) return null;
  const spot: CSSProperties | undefined = rect
    ? { left: rect.left - 4, top: rect.top - 4, width: rect.width + 8, height: rect.height + 8 }
    : undefined;
  const placement = current.placement || "bottom";
  return (
    <div className="fixed inset-0 z-[60]">
      <div className="rv-overlay-backdrop absolute inset-0 bg-black/55" aria-hidden="true" onClick={onClose} />
      {spot ? (
        <div
          className="pointer-events-none fixed rounded-md ring-2 ring-accent ring-offset-2 ring-offset-background"
          style={spot}
          aria-hidden="true"
        />
      ) : null}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        tabIndex={-1}
        className="fixed z-10 w-[min(24rem,calc(100vw-1.5rem))] rounded-lg border border-border bg-card p-4 shadow-xl"
        style={cardStyle(rect, placement)}
      >
        <div aria-live="polite">
          <p className="text-xs font-medium text-muted-foreground">Step {index + 1} of {steps.length}</p>
          <h2 id={titleId} className="mt-1 text-base font-semibold">{current.title}</h2>
          <p id={bodyId} className="mt-2 text-sm text-muted-foreground">{current.body}</p>
        </div>
        <div className="mt-4 flex items-center gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onComplete}>
            Skip tour
          </Button>
          <div className="ml-auto flex gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={nextAvailable(index, -1) == null}
              onClick={() => {
                const previous = nextAvailable(index, -1);
                if (previous != null) setIndex(previous);
              }}
            >
              Back
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                if (isLast) {
                  onComplete();
                  return;
                }
                const next = nextAvailable(index, 1);
                if (next != null) setIndex(next);
                else onComplete();
              }}
            >
              {isLast ? "Done" : "Next"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
