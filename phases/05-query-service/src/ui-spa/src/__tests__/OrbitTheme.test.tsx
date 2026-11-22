import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  ORBIT_THEME_STORAGE_KEY,
  OrbitThemeProvider,
  OrbitThemeToggle,
  useOrbitTheme,
} from "../components/OrbitTheme";

function ThemeProbe() {
  const { theme, setTheme } = useOrbitTheme();
  return (
    <div>
      <output aria-label="Active theme">{theme}</output>
      <button type="button" onClick={() => setTheme("light")}>Use light</button>
      <OrbitThemeToggle />
    </div>
  );
}

function renderTheme() {
  return render(
    <OrbitThemeProvider>
      <ThemeProbe />
    </OrbitThemeProvider>
  );
}

describe("OrbitThemeProvider", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove("dark");
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.removeProperty("color-scheme");
    document.head.innerHTML = `
      <meta name="theme-color" content="#0a0a0a" data-dark="#0a0a0a" data-light="#fafafa" />
    `;
  });

  it("defaults missing or invalid preferences to dark", () => {
    localStorage.setItem(ORBIT_THEME_STORAGE_KEY, "system");

    renderTheme();

    expect(screen.getByLabelText("Active theme")).toHaveTextContent("dark");
    expect(document.documentElement).toHaveClass("dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute("content", "#0a0a0a");
  });

  it("applies a valid stored preference on first render", () => {
    localStorage.setItem(ORBIT_THEME_STORAGE_KEY, "light");

    renderTheme();

    expect(screen.getByLabelText("Active theme")).toHaveTextContent("light");
    expect(document.documentElement).not.toHaveClass("dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute("content", "#fafafa");
  });

  it("switches, applies, and persists from the accessible header action", () => {
    renderTheme();

    const toggle = screen.getByRole("button", { name: "Switch to light theme" });
    fireEvent.click(toggle);

    expect(screen.getByLabelText("Active theme")).toHaveTextContent("light");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
    expect(localStorage.getItem(ORBIT_THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement).not.toHaveClass("dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute("content", "#fafafa");
  });

  it("exposes setTheme with the same immediate persistence contract", () => {
    renderTheme();

    fireEvent.click(screen.getByRole("button", { name: "Use light" }));

    expect(localStorage.getItem(ORBIT_THEME_STORAGE_KEY)).toBe("light");
    expect(screen.getByLabelText("Active theme")).toHaveTextContent("light");
  });

  it("synchronizes theme changes and removals from another tab", () => {
    renderTheme();

    fireEvent(window, new StorageEvent("storage", {
      key: ORBIT_THEME_STORAGE_KEY,
      newValue: "light",
      storageArea: sessionStorage,
    }));
    expect(screen.getByLabelText("Active theme")).toHaveTextContent("dark");

    fireEvent(window, new StorageEvent("storage", {
      key: ORBIT_THEME_STORAGE_KEY,
      newValue: "light",
      storageArea: localStorage,
    }));
    expect(screen.getByLabelText("Active theme")).toHaveTextContent("light");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");

    fireEvent(window, new StorageEvent("storage", {
      key: ORBIT_THEME_STORAGE_KEY,
      newValue: null,
      storageArea: localStorage,
    }));
    expect(screen.getByLabelText("Active theme")).toHaveTextContent("dark");
    expect(document.documentElement).toHaveClass("dark");
  });

  it("keeps working when browser storage is unavailable", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    renderTheme();
    fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));

    expect(screen.getByLabelText("Active theme")).toHaveTextContent("light");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    getItem.mockRestore();
    setItem.mockRestore();
  });
});
