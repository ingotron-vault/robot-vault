import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { cn } from "../cn";
import { Tone } from "../status";

export interface ToastItem {
  id: number;
  message: string;
  tone: Tone;
}

interface ToastContextValue {
  toast: (message: string, tone?: Tone) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

// Tone lives on the border + a tinted fill; the label uses the tone's
// `-foreground` token rather than the raw tone color, which measured as low
// as ~1.8:1 for `warning` in light mode over this same 10% tint (see
// phases/ui-kit/src/tokens.css's header comment). `--success-foreground` /
// `--warning-foreground` / `--danger-foreground` hold >=4.5:1 against it in
// both themes.
const TONE_CLASSES: Record<Tone, string> = {
  neutral: "border-border bg-card text-foreground",
  info: "border-accent/30 bg-accent/10 text-accent",
  success: "border-success/30 bg-success/10 text-success-foreground",
  danger: "border-danger/30 bg-danger/10 text-danger-foreground",
  warning: "border-warning/30 bg-warning/10 text-warning-foreground",
};

const AUTO_DISMISS_MS = 4000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  // Auto-dismiss timers outlived the provider: nothing cancelled them, so a
  // toast raised shortly before unmount fired its `setItems` into a component
  // that no longer exists. Under Vitest that surfaces as a `window is not
  // defined` error thrown after the jsdom environment is torn down, which a CI
  // configured to fail on unhandled errors would break on.
  const timers = useRef<number[]>([]);

  useEffect(
    () => () => {
      for (const timer of timers.current) window.clearTimeout(timer);
      timers.current = [];
    },
    []
  );

  const toast = useCallback((message: string, tone: Tone = "neutral") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev, { id, message, tone }]);
    const timer = window.setTimeout(() => {
      timers.current = timers.current.filter((entry) => entry !== timer);
      setItems((prev) => prev.filter((item) => item.id !== id));
    }, AUTO_DISMISS_MS);
    timers.current.push(timer);
  }, []);

  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="fixed bottom-4 right-4 z-50 flex flex-col gap-2"
        aria-live="polite"
        aria-atomic="false"
      >
        {items.map((item) => (
          <div
            key={item.id}
            role="status"
            className={cn("rounded-md border px-3 py-2 text-sm shadow-lg", TONE_CLASSES[item.tone])}
          >
            {item.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}
