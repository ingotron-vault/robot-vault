import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const authMock = vi.hoisted(() => ({
  snapshot: { ready: true, mode: "oidc" as string | null, signedIn: false, error: null as string | null },
  initializeAuth: vi.fn(),
  beginSignIn: vi.fn(),
  signOut: vi.fn(),
}));

vi.mock("../lib/auth", () => ({
  beginSignIn: authMock.beginSignIn,
  getAuthSnapshot: () => authMock.snapshot,
  initializeAuth: authMock.initializeAuth,
  signOut: authMock.signOut,
  subscribeAuth: () => () => {},
}));

import AuthControl from "../components/AuthControl";
import { renderWithProviders } from "./testUtils";

beforeEach(() => {
  authMock.snapshot = { ready: true, mode: "oidc", signedIn: false, error: null };
  authMock.initializeAuth.mockReset().mockResolvedValue(undefined);
  authMock.beginSignIn.mockReset();
  authMock.signOut.mockReset().mockResolvedValue(null);
});

describe("AuthControl", () => {
  it("offers sign-out for an active session and clears it through the auth layer", async () => {
    authMock.snapshot = { ready: true, mode: "oidc", signedIn: true, error: null };
    renderWithProviders(<AuthControl />);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(authMock.signOut).toHaveBeenCalledOnce();
  });

  it("labels explicitly enabled development auth instead of showing a real login", () => {
    authMock.snapshot = { ready: true, mode: "dev", signedIn: true, error: null };
    renderWithProviders(<AuthControl />);
    expect(screen.getByText("Development auth")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign/i })).not.toBeInTheDocument();
  });

  it("keeps the configured local-access mode visible without adding a login", () => {
    authMock.snapshot = { ready: true, mode: "none", signedIn: false, error: null };
    renderWithProviders(<AuthControl />);
    expect(screen.getByText("Local access")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign/i })).not.toBeInTheDocument();
  });

  it("offers a retry when authentication configuration is unavailable", async () => {
    authMock.snapshot = { ready: true, mode: null, signedIn: false, error: "Authentication unavailable" };
    renderWithProviders(<AuthControl />);
    await userEvent.click(screen.getByRole("button", { name: "Retry authentication" }));
    expect(authMock.initializeAuth).toHaveBeenCalledTimes(2);
  });
});
