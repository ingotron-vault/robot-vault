import { ReactNode, createContext, useContext } from "react";

interface OnboardingActions {
  startTour: () => void;
  openHelp: () => void;
}

const OnboardingContext = createContext<OnboardingActions | null>(null);

export function OnboardingProvider({ children, value }: { children: ReactNode; value: OnboardingActions }) {
  return <OnboardingContext.Provider value={value}>{children}</OnboardingContext.Provider>;
}

/** Null when a page is rendered outside the shell, as unit tests often are. */
export function useOnboarding(): OnboardingActions | null {
  return useContext(OnboardingContext);
}
