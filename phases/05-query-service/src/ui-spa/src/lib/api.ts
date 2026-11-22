import { authorizedFetch } from "./auth";

export class HttpError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`HTTP ${status}`);
    this.name = "HttpError";
    this.status = status;
    this.detail = detail;
  }
}

export async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await authorizedFetch(path, { ...init, headers });
  if (!response.ok) {
    let detail: unknown;
    try {
      // FastAPI raises `{"detail": …}`; the ingest provisioning API this
      // service proxies answers `{"error": "<code>"}` with no `detail` at all.
      // Reading only `detail` silently discarded every
      // provisioning refusal code -- `too_many_open_codes`,
      // `robot_decommissioned` -- leaving callers with a bare status number.
      // The fallback only fires where `detail` was already `undefined`.
      const payload = await response.json() as { detail?: unknown; error?: unknown };
      detail = payload?.detail ?? payload?.error;
    } catch {
      detail = undefined;
    }
    throw new HttpError(response.status, detail);
  }
  return response.json() as Promise<T>;
}
