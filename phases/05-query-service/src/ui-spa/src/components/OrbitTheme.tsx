import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "@robot-vault/ui-kit";

export type OrbitTheme = "dark" | "light";

export const ORBIT_THEME_STORAGE_KEY = "robot-vault.theme";

export interface OrbitThemeValue {
  theme: OrbitTheme;
  setTheme: (theme: OrbitTheme) => void;
  toggleTheme: () => void;
}

const OrbitThemeContext = createContext<OrbitThemeValue | null>(null);
const useBrowserLayoutEffect = typeof document === "undefined" ? useEffect : useLayoutEffect;

function isOrbitTheme(value: unknown): value is OrbitTheme {
  return value === "dark" || value === "light";
}

function readStoredTheme(): OrbitTheme {
  if (typeof window === "undefined") return "dark";

  try {
    const stored = window.localStorage.getItem(ORBIT_THEME_STORAGE_KEY);
    return isOrbitTheme(stored) ? stored : "dark";
  } catch {
    return "dark";
  }
}

function persistTheme(theme: OrbitTheme) {
  if (typeof window === "undefined") return;

  try {
    window.localStorage.setItem(ORBIT_THEME_STORAGE_KEY, theme);
  } catch {
    // The UI still switches when storage is unavailable (for example, in a
    // locked-down or private browser context); it simply cannot persist.
  }
}

function applyTheme(theme: OrbitTheme) {
  if (typeof document === "undefined") return;

  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.dataset.theme = theme;
  root.style.colorScheme = theme;

  const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  const color = themeColor?.getAttribute(`data-${theme}`);
  if (themeColor && color) themeColor.content = color;
}

export function OrbitThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<OrbitTheme>(readStoredTheme);

  const setTheme = useCallback((nextTheme: OrbitTheme) => {
    if (!isOrbitTheme(nextTheme)) return;

    // Apply and persist in the same interaction turn. State then keeps every
    // React consumer (including canvas charts) synchronized with the DOM.
    applyTheme(nextTheme);
    persistTheme(nextTheme);
    setThemeState(nextTheme);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [setTheme, theme]);

  useBrowserLayoutEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const handleStorage = (event: StorageEvent) => {
      if (event.storageArea) {
        try {
          if (event.storageArea !== window.localStorage) return;
        } catch {
          return;
        }
      }
      if (event.key !== ORBIT_THEME_STORAGE_KEY && event.key !== null) return;
      const nextTheme = isOrbitTheme(event.newValue) ? event.newValue : "dark";
      applyTheme(nextTheme);
      setThemeState(nextTheme);
    };

    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme }),
    [setTheme, theme, toggleTheme]
  );

  return <OrbitThemeContext.Provider value={value}>{children}</OrbitThemeContext.Provider>;
}

/** Add a provider only when a component is rendered outside the app root. */
export function OrbitThemeBoundary({ children }: { children: ReactNode }) {
  const existingTheme = useContext(OrbitThemeContext);
  return existingTheme
    ? <>{children}</>
    : <OrbitThemeProvider>{children}</OrbitThemeProvider>;
}

export function useOrbitTheme(): OrbitThemeValue {
  const value = useContext(OrbitThemeContext);
  if (!value) {
    throw new Error("useOrbitTheme must be used within an OrbitThemeProvider");
  }
  return value;
}

export function OrbitThemeToggle() {
  const { theme, toggleTheme } = useOrbitTheme();
  const isDark = theme === "dark";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <Button
      type="button"
      className="rv-shell__theme"
      variant="ghost"
      size="sm"
      aria-label={label}
      title={label}
      onClick={toggleTheme}
    >
      {isDark
        ? <Sun size={16} aria-hidden="true" />
        : <Moon size={16} aria-hidden="true" />}
    </Button>
  );
}
