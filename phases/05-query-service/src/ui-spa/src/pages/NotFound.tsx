import { Link, useLocation } from "react-router-dom";
import { PageHeader } from "../components/PageHeader";
import "./NotFound.css";

export default function NotFound() {
  const location = useLocation();
  return (
    <section className="rv-not-found" aria-labelledby="not-found-title">
      <PageHeader
        kicker="Unknown route"
        title={<span id="not-found-title">Page not found</span>}
        description={<>Orbit has no page at <code>{location.pathname}</code>. Choose a task below or return to the fleet overview.</>}
      />
      <nav aria-label="Page not found actions">
        <Link className="is-primary" to="/">Fleet overview</Link>
        <Link to="/uploads">Check intake</Link>
        <Link to="/explore">Explore data</Link>
        <Link to="/help">Open help</Link>
      </nav>
    </section>
  );
}
