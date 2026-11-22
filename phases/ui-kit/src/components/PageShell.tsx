import { ReactNode, useEffect, useRef } from "react";
import { cn } from "../cn";

/*
 * Sticky translucent app header + centered main column, ported from
 * phase-05's Layout.tsx. Deliberately has no react-router dependency so
 * both SPAs (and any router, or none) can use it — callers pass their own
 * router links/outlet in via `nav`/`children`.
 */
export interface PageShellProps {
  title: ReactNode;
  nav?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}

export function PageShell({ title, nav, actions, children, className }: PageShellProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLElement>(null);

  // Publishes the header's real rendered height as `--app-header-h` on this
  // subtree, so any page content below it (e.g. a page-level sticky filter
  // bar) can anchor to `top: var(--app-header-h)` instead of a hardcoded
  // pixel guess. Recomputed via ResizeObserver so it stays correct if the
  // header's content/height ever changes (nav wrapping, theme, font load).
  useEffect(() => {
    const headerEl = headerRef.current;
    const rootEl = rootRef.current;
    if (!headerEl || !rootEl) return;
    const setVar = () => {
      rootEl.style.setProperty("--app-header-h", `${headerEl.offsetHeight}px`);
    };
    setVar();
    const ro = new ResizeObserver(setVar);
    ro.observe(headerEl);
    return () => ro.disconnect();
  }, []);

  return (
    <div ref={rootRef} className={cn("min-h-screen", className)}>
      <header
        ref={headerRef}
        className="sticky top-0 z-10 border-b border-border bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60"
      >
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
          <strong className="text-lg">{title}</strong>
          {nav ? <nav className="flex items-center gap-4 text-sm text-muted-foreground">{nav}</nav> : null}
          {actions}
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
    </div>
  );
}
