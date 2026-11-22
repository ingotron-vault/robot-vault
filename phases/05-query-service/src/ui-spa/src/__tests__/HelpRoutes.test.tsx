import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({ http: vi.fn() }));

import { http } from "../lib/api";
import HelpPage from "../pages/Help";
import { renderWithProviders } from "./testUtils";

const httpMock = http as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  httpMock.mockResolvedValue({ edition: "pro" });
});

describe("local help and route recovery", () => {
  it("replaces Pro-only workflows with the Available in Pro panel in this core build", async () => {
    httpMock.mockResolvedValue({ edition: "core" });
    renderWithProviders(<HelpPage />, { route: "/help" });

    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Open Review" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Open Verification" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "Open Intake" })).toBeInTheDocument();
    expect(screen.getAllByTestId("available-in-pro")).toHaveLength(2);
  });

});
