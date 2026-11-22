import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@robot-vault/ui-kit";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    discoverRobots: vi.fn().mockResolvedValue({ robots: [] }),
    getJobs: vi.fn().mockResolvedValue([]),
  };
});

import App from "../App";

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider><App /></ToastProvider>
    </QueryClientProvider>
  );
}

beforeAll(() => {
  if (typeof window.matchMedia !== "function") {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }
  if (typeof ResizeObserver === "undefined") {
    class TestResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = TestResizeObserver as unknown as typeof ResizeObserver;
  }
});

describe("App onboarding and help", () => {
  beforeEach(() => {
    window.location.hash = "#/";
  });

  it("opens the first-run tour from the app and reopens it through the help panel", async () => {
    const user = userEvent.setup();
    renderApp();

    expect(await screen.findByRole("dialog", { name: "This is your local operator console" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip tour" }));
    expect(localStorage.getItem("robot-vault:tour:robot-upload-console:completed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "Open help" }));
    expect(await screen.findByRole("dialog", { name: "Help" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Search help" })).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "Start tour" }));
    expect(await screen.findByRole("dialog", { name: "This is your local operator console" })).toBeInTheDocument();
  });
});
