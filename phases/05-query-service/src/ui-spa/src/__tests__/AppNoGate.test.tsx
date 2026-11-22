/*
 * The operator's identity provider supplies the first admin, so the app
 * renders at once, with no first-run gate. This mounts the real entry point (`main.tsx`).
 */
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

const requested: string[] = [];

vi.mock("../lib/api", () => ({
  http: vi.fn((path: string) => {
    requested.push(path);
    return Promise.resolve({ items: [], edition: "core", scope: "test", status: {} });
  }),
  HttpError: class HttpError extends Error {},
}));

describe("app start", () => {
  it("renders the app shell at once and never probes bootstrap state", async () => {
    const fetchSpy = vi.fn((input: RequestInfo | URL) => {
      requested.push(String(input));
      return Promise.resolve(new Response(JSON.stringify({ mode: "none" }), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchSpy);
    document.body.innerHTML = '<div id="root"></div>';
    window.history.pushState({}, "", "/ui/help"); // a static page: Home needs real metric shapes

    await import("../main");

    expect(await screen.findByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
    expect(requested.filter((path) => path.includes("bootstrap/state"))).toEqual([]);
    expect(screen.queryByText(/claim/i)).not.toBeInTheDocument();
  });
});
