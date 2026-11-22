const AUTH_CONFIG_PATH = "/api/v1/auth/config";
const TOKEN_SESSION_KEY = "robot-vault.oidc.tokens.v1";
const PENDING_SESSION_KEY = "robot-vault.oidc.pending.v1";
const CALLBACK_MAX_AGE_MS = 10 * 60 * 1000;
const EXPIRY_SKEW_MS = 30 * 1000;
const AUTH_NETWORK_TIMEOUT_MS = 10 * 1000;

export type RuntimeAuthConfig =
  | { mode: "none" }
  | { mode: "dev" }
  | OidcRuntimeConfig;

export interface OidcRuntimeConfig {
  mode: "oidc";
  issuer: string;
  client_id: string;
  authorization_url: string;
  token_url: string;
  end_session_url?: string;
  scopes: string[];
  callback_path: "/ui/auth/callback/";
  post_logout_path: "/ui/";
}

interface TokenSession {
  provider: string;
  accessToken: string;
  refreshToken?: string;
  expiresAt: number;
}

interface PendingAuthorization {
  provider: string;
  state: string;
  verifier: string;
  redirectUri: string;
  returnTo: string;
  createdAt: number;
}

interface TokenEndpointResponse {
  access_token: string;
  token_type: string;
  refresh_token?: string;
  expires_in?: number;
}

export interface AuthSnapshot {
  ready: boolean;
  mode: RuntimeAuthConfig["mode"] | null;
  signedIn: boolean;
  error: string | null;
}

const initialSnapshot: AuthSnapshot = {
  ready: false,
  mode: null,
  signedIn: false,
  error: null,
};

let authSnapshot = initialSnapshot;
let runtimeConfigPromise: Promise<RuntimeAuthConfig> | null = null;
let devTokenPromise: Promise<string> | null = null;
let refreshPromise: Promise<string> | null = null;
let sessionEpoch = 0;
// A storage implementation can become non-removable after a token was saved
// (privacy-mode changes, quota/security errors, test doubles).  Treat a local
// clear as authoritative for this page even when the stale bytes remain in
// sessionStorage; only a later, verified write may make them usable again.
let tokenSessionRevoked = false;
const listeners = new Set<() => void>();

function publish(next: AuthSnapshot): void {
  authSnapshot = next;
  for (const listener of listeners) listener();
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getAuthSnapshot(): AuthSnapshot {
  return authSnapshot;
}

function storageGet(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): boolean {
  try {
    sessionStorage.setItem(key, value);
    return sessionStorage.getItem(key) === value;
  } catch {
    return false;
  }
}

function storageRemove(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // Nothing else can make unavailable storage more cleared.
  }
}

function readTokenSession(): TokenSession | null {
  if (tokenSessionRevoked) return null;
  const raw = storageGet(TOKEN_SESSION_KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<TokenSession>;
    if (
      typeof value.provider !== "string" ||
      typeof value.accessToken !== "string" ||
      !value.accessToken ||
      typeof value.expiresAt !== "number" ||
      !Number.isFinite(value.expiresAt) ||
      (value.refreshToken !== undefined && typeof value.refreshToken !== "string")
    ) {
      throw new Error("invalid token session");
    }
    return value as TokenSession;
  } catch {
    storageRemove(TOKEN_SESSION_KEY);
    return null;
  }
}

function writeTokenSession(value: TokenSession): boolean {
  const stored = storageSet(TOKEN_SESSION_KEY, JSON.stringify(value));
  if (stored) tokenSessionRevoked = false;
  return stored;
}

function clearTokenSession(): void {
  sessionEpoch += 1;
  tokenSessionRevoked = true;
  storageRemove(TOKEN_SESSION_KEY);
  devTokenPromise = null;
  refreshPromise = null;
}

function providerKey(config: RuntimeAuthConfig): string {
  if (config.mode === "dev") return "dev";
  if (config.mode === "oidc") return `${config.issuer}\n${config.client_id}`;
  return "none";
}

function matchingTokenSession(config: RuntimeAuthConfig): TokenSession | null {
  const session = readTokenSession();
  if (!session) return null;
  if (session.provider !== providerKey(config)) {
    clearTokenSession();
    return null;
  }
  return session;
}

function updateSnapshot(config: RuntimeAuthConfig, error: string | null = null): void {
  publish({
    ready: true,
    mode: config.mode,
    signedIn: config.mode === "dev" || Boolean(matchingTokenSession(config)),
    error,
  });
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseHttpsUrl(value: unknown, field: string): URL {
  if (typeof value !== "string" || !value) throw new Error(`Invalid ${field}`);
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.hash) {
    throw new Error(`Invalid ${field}`);
  }
  return parsed;
}

function parseRuntimeConfig(value: unknown): RuntimeAuthConfig {
  if (!isPlainObject(value)) throw new Error("Invalid authentication configuration");
  if (value.mode === "none") return { mode: "none" };
  if (value.mode === "dev") return { mode: "dev" };
  if (value.mode !== "oidc") throw new Error("Invalid authentication mode");

  const issuer = parseHttpsUrl(value.issuer, "issuer");
  const authorization = parseHttpsUrl(value.authorization_url, "authorization endpoint");
  const token = parseHttpsUrl(value.token_url, "token endpoint");
  const endSession = value.end_session_url === undefined
    ? undefined
    : parseHttpsUrl(value.end_session_url, "end-session endpoint");
  if (issuer.search) throw new Error("Invalid issuer");
  if (typeof value.client_id !== "string" || !value.client_id) {
    throw new Error("Invalid OIDC client id");
  }
  if (
    !Array.isArray(value.scopes) ||
    !value.scopes.every((scope) => typeof scope === "string" && Boolean(scope)) ||
    !value.scopes.includes("openid")
  ) {
    throw new Error("Invalid OIDC scopes");
  }
  if (value.callback_path !== "/ui/auth/callback/" || value.post_logout_path !== "/ui/") {
    throw new Error("Invalid OIDC redirect paths");
  }

  return {
    mode: "oidc",
    issuer: issuer.toString(),
    client_id: value.client_id,
    authorization_url: authorization.toString(),
    token_url: token.toString(),
    ...(endSession ? { end_session_url: endSession.toString() } : {}),
    scopes: [...value.scopes],
    callback_path: value.callback_path,
    post_logout_path: value.post_logout_path,
  };
}

async function fetchRuntimeConfig(): Promise<RuntimeAuthConfig> {
  const { response, body } = await fetchJsonWithTimeout(AUTH_CONFIG_PATH, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    referrerPolicy: "no-referrer",
  });
  if (!response.ok) throw new Error("Authentication configuration is unavailable");
  return parseRuntimeConfig(body);
}

async function fetchJsonWithTimeout(
  input: string,
  init: RequestInit,
): Promise<{ response: Response; body: unknown }> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), AUTH_NETWORK_TIMEOUT_MS);
  try {
    const response = await fetch(input, { ...init, signal: controller.signal });
    let body: unknown = null;
    try {
      body = await response.json();
    } catch (error) {
      if (controller.signal.aborted) throw error;
    }
    return { response, body };
  } finally {
    window.clearTimeout(timeout);
  }
}

export function getRuntimeAuthConfig(): Promise<RuntimeAuthConfig> {
  if (!runtimeConfigPromise) {
    runtimeConfigPromise = fetchRuntimeConfig()
      .then((config) => {
        updateSnapshot(config);
        return config;
      })
      .catch((error) => {
        // A transient outage must not turn this single-flight request into a
        // permanent failure for the lifetime of the browser tab.
        runtimeConfigPromise = null;
        throw error;
      });
  }
  return runtimeConfigPromise;
}

export async function initializeAuth(): Promise<void> {
  try {
    await getRuntimeAuthConfig();
  } catch {
    publish({ ready: true, mode: null, signedIn: false, error: "Authentication unavailable" });
  }
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomBase64Url(byteLength: number): string {
  if (!globalThis.crypto?.getRandomValues) throw new Error("Secure browser randomness is unavailable");
  const bytes = new Uint8Array(byteLength);
  globalThis.crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function pkceChallenge(verifier: string): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("PKCE requires Web Crypto");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function callbackUri(config: OidcRuntimeConfig): string {
  const uri = new URL(config.callback_path, window.location.origin);
  if (uri.origin !== window.location.origin || uri.pathname !== "/ui/auth/callback/") {
    throw new Error("Invalid OIDC callback URI");
  }
  return uri.toString();
}

export function safeReturnTo(candidate: string | undefined): string {
  const fallback = "/ui/";
  if (!candidate) return fallback;
  try {
    const parsed = new URL(candidate, window.location.origin);
    const inUi = parsed.pathname === "/ui" || parsed.pathname.startsWith("/ui/");
    if (
      parsed.origin !== window.location.origin ||
      !inUi ||
      parsed.pathname === "/ui/auth/callback" ||
      parsed.pathname === "/ui/auth/callback/"
    ) {
      return fallback;
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}

export async function beginSignIn(returnTo?: string): Promise<string> {
  const config = await getRuntimeAuthConfig();
  if (config.mode !== "oidc") throw new Error("OIDC sign-in is not enabled");

  const verifier = randomBase64Url(32); // 43 chars: RFC 7636's minimum.
  const state = randomBase64Url(32);
  const redirectUri = callbackUri(config);
  const pending: PendingAuthorization = {
    provider: providerKey(config),
    state,
    verifier,
    redirectUri,
    returnTo: safeReturnTo(returnTo),
    createdAt: Date.now(),
  };
  if (!storageSet(PENDING_SESSION_KEY, JSON.stringify(pending))) {
    throw new Error("Sign-in requires session storage");
  }

  const authorization = new URL(config.authorization_url);
  authorization.searchParams.set("response_type", "code");
  authorization.searchParams.set("client_id", config.client_id);
  authorization.searchParams.set("redirect_uri", redirectUri);
  authorization.searchParams.set("scope", config.scopes.join(" "));
  authorization.searchParams.set("state", state);
  authorization.searchParams.set("code_challenge", await pkceChallenge(verifier));
  authorization.searchParams.set("code_challenge_method", "S256");
  return authorization.toString();
}

function readPendingAuthorization(): PendingAuthorization | null {
  const raw = storageGet(PENDING_SESSION_KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<PendingAuthorization>;
    if (
      typeof value.provider !== "string" ||
      typeof value.state !== "string" ||
      typeof value.verifier !== "string" ||
      typeof value.redirectUri !== "string" ||
      typeof value.returnTo !== "string" ||
      typeof value.createdAt !== "number"
    ) {
      throw new Error("invalid pending authorization");
    }
    return value as PendingAuthorization;
  } catch {
    storageRemove(PENDING_SESSION_KEY);
    return null;
  }
}

function singleCallbackParam(params: URLSearchParams, name: string): string | null {
  const values = params.getAll(name);
  if (values.length > 1) throw new Error(`Duplicate OIDC callback parameter: ${name}`);
  return values[0] || null;
}

function jwtExpiryMs(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payload.length / 4) * 4, "=");
    const claims = JSON.parse(atob(padded)) as { exp?: unknown };
    return typeof claims.exp === "number" && Number.isFinite(claims.exp) ? claims.exp * 1000 : null;
  } catch {
    return null;
  }
}

function tokenExpiry(response: TokenEndpointResponse): number {
  if (typeof response.expires_in === "number" && Number.isFinite(response.expires_in)) {
    return Date.now() + Math.max(0, response.expires_in) * 1000;
  }
  return jwtExpiryMs(response.access_token) ?? Date.now() + 5 * 60 * 1000;
}

function parseTokenResponse(value: unknown): TokenEndpointResponse {
  if (!isPlainObject(value) || typeof value.access_token !== "string" || !value.access_token) {
    throw new Error("OIDC token endpoint returned no access token");
  }
  if (typeof value.token_type !== "string" || value.token_type.toLowerCase() !== "bearer") {
    throw new Error("OIDC token endpoint returned an unsupported token type");
  }
  if (value.refresh_token !== undefined && typeof value.refresh_token !== "string") {
    throw new Error("OIDC token endpoint returned an invalid refresh token");
  }
  const expiresIn = value.expires_in === undefined ? undefined : Number(value.expires_in);
  if (expiresIn !== undefined && (!Number.isFinite(expiresIn) || expiresIn < 0)) {
    throw new Error("OIDC token endpoint returned an invalid expiry");
  }
  return {
    access_token: value.access_token,
    token_type: value.token_type,
    ...(value.refresh_token ? { refresh_token: value.refresh_token } : {}),
    ...(expiresIn !== undefined ? { expires_in: expiresIn } : {}),
  };
}

async function requestTokens(
  config: OidcRuntimeConfig,
  parameters: Record<string, string>,
): Promise<TokenEndpointResponse> {
  // token_url was supplied by the same-origin backend, validated again above,
  // and is the only off-origin destination that ever receives a code,
  // verifier, access token or refresh token.
  const { response, body } = await fetchJsonWithTimeout(config.token_url, {
    method: "POST",
    credentials: "omit",
    redirect: "error",
    referrerPolicy: "no-referrer",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(parameters).toString(),
  });
  // The status below remains the only error detail shown; IdP responses can
  // carry codes or descriptions that should not be reflected into the UI.
  if (!response.ok) throw new Error("OIDC token request failed");
  return parseTokenResponse(body);
}

function persistTokenResponse(
  config: OidcRuntimeConfig,
  response: TokenEndpointResponse,
  previousRefreshToken?: string,
): string {
  const session: TokenSession = {
    provider: providerKey(config),
    accessToken: response.access_token,
    ...(response.refresh_token || previousRefreshToken
      ? { refreshToken: response.refresh_token || previousRefreshToken }
      : {}),
    expiresAt: tokenExpiry(response),
  };
  if (!writeTokenSession(session)) {
    throw new Error("Sign-in tokens could not be saved in session storage");
  }
  updateSnapshot(config);
  return session.accessToken;
}

export async function completeSignIn(callback: string | URL): Promise<{ returnTo: string }> {
  const requestEpoch = sessionEpoch;
  const callbackUrl = typeof callback === "string" ? new URL(callback) : callback;
  if (callbackUrl.origin !== window.location.origin || callbackUrl.pathname !== "/ui/auth/callback/") {
    throw new Error("Invalid OIDC callback location");
  }

  const pending = readPendingAuthorization();
  if (!pending) throw new Error("No OIDC sign-in is pending");
  const state = singleCallbackParam(callbackUrl.searchParams, "state");
  if (!state || state !== pending.state) throw new Error("OIDC callback state did not match");
  if (Date.now() - pending.createdAt > CALLBACK_MAX_AGE_MS || pending.createdAt > Date.now() + 1000) {
    storageRemove(PENDING_SESSION_KEY);
    throw new Error("OIDC sign-in has expired");
  }

  // A matching callback is single-use, even if the IdP reports an error or
  // the code exchange fails. Retrying starts a fresh state/verifier pair.
  storageRemove(PENDING_SESSION_KEY);
  const providerError = singleCallbackParam(callbackUrl.searchParams, "error");
  if (providerError) throw new Error("The identity provider declined sign-in");
  const code = singleCallbackParam(callbackUrl.searchParams, "code");
  if (!code) throw new Error("OIDC callback did not include an authorization code");

  const config = await getRuntimeAuthConfig();
  if (
    config.mode !== "oidc" ||
    pending.provider !== providerKey(config) ||
    pending.redirectUri !== callbackUri(config)
  ) {
    throw new Error("OIDC configuration changed during sign-in");
  }
  const response = await requestTokens(config, {
    grant_type: "authorization_code",
    client_id: config.client_id,
    code,
    redirect_uri: pending.redirectUri,
    code_verifier: pending.verifier,
  });
  if (requestEpoch !== sessionEpoch) throw new Error("OIDC sign-in was cancelled");
  persistTokenResponse(config, response);
  return { returnTo: safeReturnTo(pending.returnTo) };
}

function tokenNeedsRefresh(session: TokenSession): boolean {
  return session.expiresAt <= Date.now() + EXPIRY_SKEW_MS;
}

async function refreshAccessToken(config: OidcRuntimeConfig): Promise<string> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const current = matchingTokenSession(config);
    if (!current?.refreshToken) {
      clearTokenSession();
      updateSnapshot(config);
      return "";
    }
    const requestEpoch = sessionEpoch;
    try {
      const response = await requestTokens(config, {
        grant_type: "refresh_token",
        client_id: config.client_id,
        refresh_token: current.refreshToken,
      });
      if (requestEpoch !== sessionEpoch) return "";
      return persistTokenResponse(config, response, current.refreshToken);
    } catch {
      clearTokenSession();
      updateSnapshot(config);
      return "";
    }
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function ensureDevToken(config: { mode: "dev" }, force: boolean): Promise<string> {
  const current = matchingTokenSession(config);
  if (!force && current && !tokenNeedsRefresh(current)) return current.accessToken;
  if (devTokenPromise) return devTokenPromise;
  const requestEpoch = sessionEpoch;
  devTokenPromise = fetchJsonWithTimeout("/auth/dev/token", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    referrerPolicy: "no-referrer",
  })
    .then(async ({ response, body }) => {
      if (!response.ok) return "";
      if (requestEpoch !== sessionEpoch || !isPlainObject(body)) return "";
      if (typeof body.access_token !== "string" || !body.access_token) return "";
      const stored = writeTokenSession({
        provider: "dev",
        accessToken: body.access_token,
        expiresAt: jwtExpiryMs(body.access_token) ?? Date.now() + 60 * 60 * 1000,
      });
      if (!stored) return "";
      updateSnapshot(config);
      return body.access_token;
    })
    .catch(() => "")
    .finally(() => {
      devTokenPromise = null;
    });
  return devTokenPromise;
}

export async function getAccessToken(forceRefresh = false): Promise<string> {
  let config: RuntimeAuthConfig;
  try {
    config = await getRuntimeAuthConfig();
  } catch {
    return "";
  }
  if (config.mode === "none") {
    matchingTokenSession(config); // clears a token left by a different mode
    return "";
  }
  if (config.mode === "dev") return ensureDevToken(config, forceRefresh);

  const current = matchingTokenSession(config);
  if (!current) return "";
  if (!forceRefresh && !tokenNeedsRefresh(current)) return current.accessToken;
  if (current.refreshToken) return refreshAccessToken(config);
  clearTokenSession();
  updateSnapshot(config);
  return "";
}

function assertSameOriginRequest(path: string): void {
  const target = new URL(path, window.location.origin);
  if (target.origin !== window.location.origin) {
    throw new Error("Authenticated API requests must stay on this origin");
  }
}

function requestHeaders(init: RequestInit | undefined, token: string): Headers {
  const headers = new Headers(init?.headers);
  headers.delete("Authorization");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function refreshAfterUnauthorized(usedToken: string): Promise<string> {
  let config: RuntimeAuthConfig;
  try {
    config = await getRuntimeAuthConfig();
  } catch {
    return "";
  }
  const current = matchingTokenSession(config);
  // A concurrent request already rotated the token while this response was
  // in flight. Reuse it instead of starting a second refresh grant.
  if (current && current.accessToken !== usedToken && !tokenNeedsRefresh(current)) {
    return current.accessToken;
  }
  return getAccessToken(true);
}

export async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  assertSameOriginRequest(path);
  const token = await getAccessToken();
  const response = await fetch(path, {
    ...init,
    redirect: "error",
    headers: requestHeaders(init, token),
  });
  if (response.status !== 401 || !token) return response;

  const replacement = await refreshAfterUnauthorized(token);
  if (!replacement) return response;
  return fetch(path, {
    ...init,
    redirect: "error",
    headers: requestHeaders(init, replacement),
  });
}

export async function signOut(): Promise<string | null> {
  clearTokenSession();
  storageRemove(PENDING_SESSION_KEY);
  let config: RuntimeAuthConfig;
  try {
    config = await getRuntimeAuthConfig();
    updateSnapshot(config);
  } catch {
    publish({ ready: true, mode: null, signedIn: false, error: "Authentication unavailable" });
    return null;
  }
  if (config.mode !== "oidc" || !config.end_session_url) return null;

  const endSession = new URL(config.end_session_url);
  endSession.searchParams.set("client_id", config.client_id);
  endSession.searchParams.set(
    "post_logout_redirect_uri",
    new URL(config.post_logout_path, window.location.origin).toString(),
  );
  // Deliberately no id_token_hint: no token is sent to a navigation endpoint.
  return endSession.toString();
}

/** Reset module-level single-flight/cache state between focused Vitest cases. */
export function resetAuthForTests(): void {
  runtimeConfigPromise = null;
  devTokenPromise = null;
  refreshPromise = null;
  sessionEpoch += 1;
  tokenSessionRevoked = true;
  storageRemove(TOKEN_SESSION_KEY);
  storageRemove(PENDING_SESSION_KEY);
  authSnapshot = initialSnapshot;
  listeners.clear();
}
