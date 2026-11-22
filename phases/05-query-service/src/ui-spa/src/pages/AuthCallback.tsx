import { useEffect, useRef, useState } from "react";
import { DatabaseZap } from "lucide-react";
import { Link } from "react-router-dom";
import { completeSignIn } from "../lib/auth";

export default function AuthCallback() {
  const started = useRef(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    // React.StrictMode re-runs effects in development. The authorization code
    // and pending state are deliberately single-use, so exchange once.
    if (started.current) return;
    started.current = true;
    void completeSignIn(window.location.href)
      .then(({ returnTo }) => window.location.replace(returnTo))
      .catch(() => setError(true));
  }, []);

  return (
    <main className="rv-auth-callback">
      <section className="rv-auth-card" aria-labelledby="auth-callback-title">
        <span className="rv-auth-mark" aria-hidden="true"><DatabaseZap size={20} /></span>
        <p className="rv-auth-product">Robot Vault</p>
        <h1 id="auth-callback-title">{error ? "Sign-in failed" : "Completing sign-in…"}</h1>
        {error ? (
          <p>
            The callback was invalid, expired, or the identity provider rejected it.{" "}
            <Link to="/">Return to Robot Vault</Link> and try again.
          </p>
        ) : (
          <p>Verifying the response from your identity provider.</p>
        )}
      </section>
    </main>
  );
}
