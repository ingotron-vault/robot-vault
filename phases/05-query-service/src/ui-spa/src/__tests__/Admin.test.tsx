import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Preserve the real `HttpError` export (Admin narrows `ingest.error` with
// `instanceof HttpError`) while replacing only `http` with a mock.
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, http: vi.fn() };
});

import Admin from "../pages/Admin";
import { http, HttpError } from "../lib/api";
import { renderWithProviders } from "./testUtils";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  httpMock.mockReset();
});

const INGEST = {
  uploads_counts: { completed: 2 },
  processing_counts: { completed: 1 },
  processed_count: 2,
  robots_count: 3,
  recent_uploads: [],
};

function ingestThen(ingest: () => Promise<unknown>) {
  httpMock.mockImplementation(() => ingest());
}

describe("Admin ingest status", () => {
  it("renders an explicit outage with a working retry instead of zero counts", async () => {
    let ingestAttempts = 0;
    ingestThen(() => {
      ingestAttempts += 1;
      return ingestAttempts === 1 ? Promise.reject(new Error("backend unavailable")) : Promise.resolve(INGEST);
    });
    const user = userEvent.setup();

    renderWithProviders(<Admin />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Ingest status unavailable. Check query-service connectivity and retry."
    );
    expect(screen.queryByText("No upload counts reported.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("across 1 state")).toBeInTheDocument();
    expect(httpMock.mock.calls.filter(([path]) => path === "/api/admin/ingest/status")).toHaveLength(2);
  });

  it("surfaces the real backend failure detail instead of a generic message", async () => {
    ingestThen(() => Promise.reject(new HttpError(503, "postgres unreachable")));

    renderWithProviders(<Admin />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("postgres unreachable");
    expect(
      screen.queryByText(
        "Ingest status unavailable. Check query-service connectivity and retry."
      )
    ).not.toBeInTheDocument();
    expect(screen.queryByText("No upload counts reported.")).not.toBeInTheDocument();
  });

  it("has no settings editor: the host manager it talked to no longer exists", async () => {
    ingestThen(() => Promise.resolve(INGEST));
    renderWithProviders(<Admin />);
    expect(await screen.findByText("across 1 state")).toBeInTheDocument();
    expect(httpMock.mock.calls.every(([path]) => path === "/api/admin/ingest/status")).toBe(true);
    expect(screen.queryByRole("button", { name: "Review changes" })).not.toBeInTheDocument();
  });
});
