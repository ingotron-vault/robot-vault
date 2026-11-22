import { useEffect, useState, useSyncExternalStore } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@robot-vault/ui-kit";
import {
  beginSignIn,
  getAuthSnapshot,
  initializeAuth,
  signOut,
  subscribeAuth,
} from "../lib/auth";

export default function AuthControl() {
  const auth = useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  useEffect(() => {
    void initializeAuth();
  }, []);

  if (!auth.ready) {
    return <span className="rv-shell__auth-status text-xs text-muted" aria-label="Authentication loading">Auth…</span>;
  }
  if (auth.error || actionError || auth.mode === null) {
    return <span className="rv-shell__auth-status inline-flex items-center gap-1 text-xs text-destructive" title="Check the query service authentication configuration">
      Auth unavailable
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => {
        setBusy(true);
        setActionError(false);
        void initializeAuth().finally(() => setBusy(false));
      }}>
        {busy ? "Retrying…" : "Retry authentication"}
      </Button>
    </span>;
  }
  if (auth.mode === "none") {
    return (
      <span className="rv-shell__auth-status text-xs text-muted" title="The viewer is configured for local access without authentication">
        Local access
      </span>
    );
  }
  if (auth.mode === "dev") {
    return <span className="rv-shell__auth-status text-xs text-muted" title="The backend explicitly enabled OIDC_DEV">Development auth</span>;
  }

  const onSignIn = async () => {
    setBusy(true);
    setActionError(false);
    try {
      const destination = `/ui${location.pathname === "/" ? "/" : location.pathname}${location.search}${location.hash}`;
      window.location.assign(await beginSignIn(destination));
    } catch {
      setActionError(true);
      setBusy(false);
    }
  };

  const onSignOut = async () => {
    setBusy(true);
    setActionError(false);
    const endSessionUrl = await signOut();
    // Authenticated query data must not remain visible in React Query's
    // in-memory cache after the bearer session is cleared.
    queryClient.clear();
    if (endSessionUrl) {
      window.location.assign(endSessionUrl);
      return;
    }
    navigate("/", { replace: true });
    setBusy(false);
  };

  return auth.signedIn ? (
    <Button size="sm" variant="ghost" disabled={busy} onClick={() => void onSignOut()}>
      {busy ? "Signing out…" : "Sign out"}
    </Button>
  ) : (
    <Button size="sm" variant="secondary" disabled={busy} onClick={() => void onSignIn()}>
      {busy ? "Redirecting…" : "Sign in"}
    </Button>
  );
}
