import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/auth", () => ({ authorizedFetch: vi.fn() }));

import { authorizedFetch } from "../lib/auth";
import { http, HttpError } from "../lib/api";

const fetchMock = authorizedFetch as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  fetchMock.mockReset();
});

describe("http error contract", () => {
  it("preserves a structured stable detail without exposing response text", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({
        detail: { code: "result_limit_exceeded", max_rows: 10_000 },
      }),
    });

    const request = http("/api/v1/query/sql");

    await expect(request).rejects.toMatchObject({
      name: "HttpError",
      message: "HTTP 422",
      status: 422,
      detail: { code: "result_limit_exceeded", max_rows: 10_000 },
    });
  });

  it("keeps a non-JSON failure generic", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => { throw new SyntaxError("not JSON"); },
    });

    await expect(http("/api/obs/alerts")).rejects.toEqual(
      new HttpError(502, undefined)
    );
  });
});
