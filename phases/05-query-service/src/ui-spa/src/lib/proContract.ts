import type { ComponentType, LazyExoticComponent, ReactNode } from "react";

/** What the Home page needs from Pro to show recoverable dataset exports. */
export interface HomeAttentionState {
  /** Export exceptions that need an operator (dead letter or expired lease). */
  count: number;
  /** Export status could not be read yet (loading or failed). */
  unknown: boolean;
  /** Reading dataset export status failed. */
  error: boolean;
  /** Reads dataset export status again. */
  retry: () => void;
  /** Message after a recovery action, or null. */
  notice: string | null;
  /** The recovery rows for the "Needs attention" list. */
  rows: ReactNode;
  /** Small chip for the Verification tile, or null. */
  tileFact: ReactNode;
}

export const NO_HOME_ATTENTION: HomeAttentionState = {
  count: 0,
  unknown: false,
  error: false,
  retry: () => {},
  notice: null,
  rows: null,
  tileFact: null,
};

/**
 * The seam between the core UI and the Pro UI. Core ships `src/pro/index.ts`
 * with `installed = false`; the Pro build replaces that file.
 */
export interface ProModule {
  installed: boolean;
  /** Set by the Pro build; lets release checks find the Pro bundle. */
  sentinel?: string;
  routes: Partial<Record<"episodes" | "datasets", LazyExoticComponent<ComponentType>>>;
  /** Renders `children` with the Home attention state. Null in core. */
  HomeAttention: ComponentType<{ children: (state: HomeAttentionState) => ReactNode }> | null;
}
