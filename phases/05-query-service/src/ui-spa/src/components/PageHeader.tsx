import type { ReactNode } from "react";
import "./PageHeader.css";

/*
 * The heading every Orbit page uses: a mono kicker that names where the page
 * sits ("03 · Review", "System / Robots"), the h1, one line of purpose, and an
 * optional action or status cluster on the right. It sets no outer margin; the
 * page layout owns the space between the header and the first section.
 */
export function PageHeader({
  kicker,
  title,
  description,
  actions,
}: {
  kicker: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="rv-page-header">
      <div className="rv-page-header__titles">
        <p className="rv-page-header__kicker">{kicker}</p>
        <h1 className="rv-page-header__title">{title}</h1>
        {description ? <p className="rv-page-header__description">{description}</p> : null}
      </div>
      {actions ? <div className="rv-page-header__actions">{actions}</div> : null}
    </header>
  );
}

/*
 * A quiet live-state note for the header's action cluster ("Refreshes every
 * 30 seconds"). The tone colors only the dot; the words carry the meaning.
 */
export function PageStatus({
  tone = "info",
  children,
  role,
}: {
  tone?: "neutral" | "info" | "ok" | "warn" | "error";
  children: ReactNode;
  role?: "status";
}) {
  return (
    <span className={`rv-page-status is-${tone}`} role={role}>
      <i aria-hidden="true" />
      {children}
    </span>
  );
}
