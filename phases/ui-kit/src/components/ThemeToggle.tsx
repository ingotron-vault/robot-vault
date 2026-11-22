import { useEffect, useState } from "react";

function getPreferredTheme(): "light" | "dark" {
  if (typeof window === "undefined") return "light";
  const stored = localStorage.getItem("theme");
  if (stored === "dark" || stored === "light") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">(getPreferredTheme());

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") root.classList.add("dark");
    else root.classList.remove("dark");
    localStorage.setItem("theme", theme);
  }, [theme]);

  return (
    <button
      type="button"
      onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
      className="ml-auto inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted-foreground/10"
      aria-label="Toggle theme"
      title="Toggle theme"
    >
      <span>{theme === "dark" ? "Dark" : "Light"}</span>
    </button>
  );
}
