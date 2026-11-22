import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  authorizedFetch,
  beginSignIn,
  completeSignIn,
  getAccessToken,
  getAuthSnapshot,
  getRuntimeAuthConfig,
  initializeAuth,
  resetAuthForTests,
  safeReturnTo,
  signOut,
} from "../lib/auth";

const issuer = "https://idp.example/realms/vault";
const runtimeConfig = {
  mode: "oidc",
  issuer,
  client_id: "robot-vault-spa",
  authorization_url: `${issuer}/protocol/openid-connect/auth`,
  token_url: `${issuer}/protocol/openid-connect/token`,
  end_session_url: `${issuer}/protocol/openid-connect/logout`,
  scopes: ["openid", "profile", "offline_access"],
  callback_path: "/ui/auth/callback/",
  post_logout_path: "/ui/",
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function callbackUrl(state: string, code = "authorization-code"): string {
  const url = new URL("/ui/auth/callback/", window.location.origin);
  url.searchParams.set("state", state);
  url.searchParams.set("code", code);
  return url.toString();
}

beforeEach(() => {
  resetAuthForTests();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  resetAuthForTests();
});

describe("Authorization Code + PKCE", () => {
  it("builds an S256 authorization request and keeps transient state out of localStorage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeConfig));
    vi.stubGlobal("fetch", fetchMock);

    const destination = await beginSignIn("/ui/datasets?id=cut-1");
    const url = new URL(destination);

    expect(url.origin).toBe("https://idp.example");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("client_id")).toBe("robot-vault-spa");
    expect(url.searchParams.get("redirect_uri")).toBe(`${window.location.origin}/ui/auth/callback/`);
    expect(url.searchParams.get("scope")).toBe("openid profile offline_access");
    expect(url.searchParams.get("state")).toHaveLength(43);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toHaveLength(43);
    expect(sessionStorage.length).toBe(1);
    expect(localStorage.length).toBe(0);
  });

  it("rejects a mismatched state without sending the code to the IdP", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeConfig));
    vi.stubGlobal("fetch", fetchMock);
    await beginSignIn("/ui/");

    await expect(completeSignIn(callbackUrl("attacker-state"))).rejects.toThrow("state did not match");
    expect(fetchMock).toHaveBeenCalledTimes(1); // same-origin runtime config only
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/v1/auth/config");
  });

  it("exchanges a matching callback once, with verifier and no client secret", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      if (url === runtimeConfig.token_url) {
        return Promise.resolve(jsonResponse({
          access_token: "access-one",
          refresh_token: "refresh-one",
          token_type: "Bearer",
          expires_in: 3600,
        }));
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const authorization = new URL(await beginSignIn("/ui/datasets?id=cut-1"));
    const state = authorization.searchParams.get("state")!;
    const result = await completeSignIn(callbackUrl(state));

    expect(result).toEqual({ returnTo: "/ui/datasets?id=cut-1" });
    const tokenCall = fetchMock.mock.calls.find(([url]) => url === runtimeConfig.token_url)!;
    const tokenInit = tokenCall[1] as RequestInit;
    expect(tokenInit.signal).toBeInstanceOf(AbortSignal);
    expect(tokenInit.redirect).toBe("error");
    const body = new URLSearchParams(String(tokenInit.body));
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code")).toBe("authorization-code");
    expect(body.get("code_verifier")).toHaveLength(43);
    expect(body.get("redirect_uri")).toBe(`${window.location.origin}/ui/auth/callback/`);
    expect(body.has("client_secret")).toBe(false);
    expect(localStorage.length).toBe(0);
    expect([...Array(sessionStorage.length)].map((_, i) => sessionStorage.key(i))).not.toContain("access_token");
    expect(sessionStorage.length).toBe(1); // token session; pending state was consumed
  });

  it("refuses to redirect when PKCE state cannot be persisted for the callback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(runtimeConfig)));
    await getRuntimeAuthConfig();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    await expect(beginSignIn("/ui/")).rejects.toThrow("requires session storage");
    expect(sessionStorage.length).toBe(0);
  });

  it("does not claim sign-in succeeded when the exchanged tokens cannot be persisted", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      return Promise.resolve(jsonResponse({
        access_token: "access-one",
        refresh_token: "refresh-one",
        token_type: "Bearer",
        expires_in: 3600,
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const authorization = new URL(await beginSignIn("/ui/"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    await expect(
      completeSignIn(callbackUrl(authorization.searchParams.get("state")!)),
    ).rejects.toThrow("could not be saved");
    expect(sessionStorage.length).toBe(0);
  });

  it("single-flights concurrent refresh grants and preserves refresh-token rotation", async () => {
    let resolveRefresh: ((response: Response) => void) | undefined;
    let refreshCalls = 0;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      if (url !== runtimeConfig.token_url) throw new Error(`unexpected fetch: ${url}`);
      const body = new URLSearchParams(String(init?.body));
      if (body.get("grant_type") === "authorization_code") {
        return Promise.resolve(jsonResponse({
          access_token: "expired-access",
          refresh_token: "refresh-one",
          token_type: "Bearer",
          expires_in: 0,
        }));
      }
      refreshCalls += 1;
      return new Promise<Response>((resolve) => {
        resolveRefresh = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const authorization = new URL(await beginSignIn("/ui/"));
    await completeSignIn(callbackUrl(authorization.searchParams.get("state")!));

    const first = getAccessToken();
    const second = getAccessToken();
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    resolveRefresh!(jsonResponse({
      access_token: "fresh-access",
      refresh_token: "refresh-two",
      token_type: "bearer",
      expires_in: 3600,
    }));

    await expect(first).resolves.toBe("fresh-access");
    await expect(second).resolves.toBe("fresh-access");
    expect(refreshCalls).toBe(1);
  });

  it("cannot resurrect a token when sign-out races an in-flight refresh", async () => {
    let resolveRefresh: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      const body = new URLSearchParams(String(init?.body));
      if (body.get("grant_type") === "authorization_code") {
        return Promise.resolve(jsonResponse({
          access_token: "expired-access",
          refresh_token: "refresh-one",
          token_type: "Bearer",
          expires_in: 0,
        }));
      }
      return new Promise<Response>((resolve) => {
        resolveRefresh = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const authorization = new URL(await beginSignIn("/ui/"));
    await completeSignIn(callbackUrl(authorization.searchParams.get("state")!));
    const refreshing = getAccessToken();
    await vi.waitFor(() => expect(resolveRefresh).toBeTypeOf("function"));
    await signOut();
    resolveRefresh!(jsonResponse({
      access_token: "must-not-survive",
      refresh_token: "refresh-two",
      token_type: "Bearer",
      expires_in: 3600,
    }));

    await expect(refreshing).resolves.toBe("");
    expect(getAuthSnapshot().signedIn).toBe(false);
    expect(sessionStorage.length).toBe(0);
  });

  it("cannot resurrect a token when sign-out races the authorization-code exchange", async () => {
    let resolveExchange: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      if (url === runtimeConfig.token_url) {
        return new Promise<Response>((resolve) => {
          resolveExchange = resolve;
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const authorization = new URL(await beginSignIn("/ui/"));
    const completing = completeSignIn(callbackUrl(authorization.searchParams.get("state")!));
    await vi.waitFor(() => expect(resolveExchange).toBeTypeOf("function"));
    await signOut();
    resolveExchange!(jsonResponse({
      access_token: "must-not-survive",
      refresh_token: "refresh-one",
      token_type: "Bearer",
      expires_in: 3600,
    }));

    await expect(completing).rejects.toThrow("cancelled");
    expect(getAuthSnapshot().signedIn).toBe(false);
    await expect(getAccessToken()).resolves.toBe("");
    expect(sessionStorage.length).toBe(0);
  });
});

describe("auth request boundaries", () => {
  it("retries runtime config after a failed first request", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ error: "unavailable" }, 503))
      .mockResolvedValueOnce(jsonResponse({ mode: "none" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getRuntimeAuthConfig()).rejects.toThrow("Authentication configuration is unavailable");
    await expect(getRuntimeAuthConfig()).resolves.toEqual({ mode: "none" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts a stalled runtime-config request after a bounded timeout", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      })
    ));
    vi.stubGlobal("fetch", fetchMock);
    const pending = getRuntimeAuthConfig();
    const assertion = expect(pending).rejects.toThrow("aborted");
    await vi.advanceTimersByTimeAsync(10_001);
    await assertion;
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
  });

  it("never probes the dev-token endpoint unless the backend reports dev mode", async () => {
    const noneFetch = vi.fn().mockResolvedValue(jsonResponse({ mode: "none" }));
    vi.stubGlobal("fetch", noneFetch);
    await expect(getAccessToken()).resolves.toBe("");
    expect(noneFetch).toHaveBeenCalledTimes(1);

    resetAuthForTests();
    const devFetch = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse({ mode: "dev" }));
      if (url === "/auth/dev/token") return Promise.resolve(jsonResponse({ access_token: "dev-access" }));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", devFetch);
    await expect(getAccessToken()).resolves.toBe("dev-access");
    expect(devFetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/auth/config",
      "/auth/dev/token",
    ]);
  });

  it("refuses to attach a bearer token to an off-origin API request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(authorizedFetch("https://evil.example/collect")).rejects.toThrow("this origin");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sanitizes stored return targets so callbacks cannot become open redirects", () => {
    expect(safeReturnTo("https://evil.example/steal")).toBe("/ui/");
    expect(safeReturnTo("//evil.example/steal")).toBe("/ui/");
    expect(safeReturnTo("/admin-outside-ui")).toBe("/ui/");
    expect(safeReturnTo("/ui/auth/callback/?loop=1")).toBe("/ui/");
    expect(safeReturnTo("/ui/auth/callback?loop=1")).toBe("/ui/");
    expect(safeReturnTo("/ui/uploads?q=robot#row")).toBe("/ui/uploads?q=robot#row");
  });

  it("clears the browser session and builds token-free optional RP logout", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeConfig));
    vi.stubGlobal("fetch", fetchMock);
    await beginSignIn("/ui/");
    const logout = new URL((await signOut())!);

    expect(logout.origin).toBe("https://idp.example");
    expect(logout.searchParams.get("client_id")).toBe("robot-vault-spa");
    expect(logout.searchParams.get("post_logout_redirect_uri")).toBe(`${window.location.origin}/ui/`);
    expect(logout.toString()).not.toContain("token");
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.length).toBe(0);
  });

  it("keeps the page signed out when session storage refuses to remove a token", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === "/api/v1/auth/config") return Promise.resolve(jsonResponse(runtimeConfig));
      if (url === runtimeConfig.token_url) {
        return Promise.resolve(jsonResponse({
          access_token: "must-stay-revoked",
          refresh_token: "refresh-one",
          token_type: "Bearer",
          expires_in: 3600,
        }));
      }
      if (url === "/api/v1/protected") return Promise.resolve(jsonResponse({ ok: true }));
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const authorization = new URL(await beginSignIn("/ui/"));
    await completeSignIn(callbackUrl(authorization.searchParams.get("state")!));
    expect(getAuthSnapshot().signedIn).toBe(true);

    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    await signOut();

    expect(getAuthSnapshot().signedIn).toBe(false);
    await expect(getAccessToken()).resolves.toBe("");
    await authorizedFetch("/api/v1/protected");
    const protectedCall = fetchMock.mock.calls.find(([url]) => url === "/api/v1/protected")!;
    expect(new Headers((protectedCall[1] as RequestInit).headers).has("Authorization")).toBe(false);
    expect(sessionStorage.length).toBe(1); // stale bytes exist but remain locally revoked
  });
});
