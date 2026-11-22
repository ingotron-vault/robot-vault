import { authorizedFetch } from "./auth";

/** Authorize once with the session bearer token, then let the browser stream
 * the scoped, single-use download directly to disk. No dataset-sized Blob,
 * storage credential, JWT in a URL, or browser-specific file picker is needed.
 */
export async function downloadAuthenticated(path: string, name: string): Promise<void> {
  const source = new URL(path, window.location.origin);
  if (source.origin !== window.location.origin) throw new Error("Download must use this vault.");
  const response = await authorizedFetch(`${source.pathname}/ticket${source.search}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, redirect: "error",
  });
  if (!response.ok) throw new Error(`Download could not start (${response.status}). Refresh the status and retry.`);
  const result = await response.json() as { url: string };
  const target = new URL(result.url, window.location.origin);
  if (target.origin !== window.location.origin || !target.pathname.startsWith("/api/v1/downloads/")) {
    throw new Error("Download URL is invalid.");
  }
  const anchor = document.createElement("a");
  anchor.href = target.pathname;
  anchor.download = name;
  anchor.referrerPolicy = "no-referrer";
  anchor.click();
}
