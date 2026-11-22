import { useEffect, useState } from "react";

// No react-router: the SPA has exactly 4 views, addressed as hash tabs so
// the app is refresh-safe by construction (the server always returns
// index.html for "/", and the hash is purely client-side).
export type Route = "/" | "/files" | "/history" | "/settings";
const VALID_ROUTES: Route[] = ["/", "/files", "/history", "/settings"];

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  return (VALID_ROUTES as string[]).includes(raw) ? (raw as Route) : "/";
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash());

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return route;
}
