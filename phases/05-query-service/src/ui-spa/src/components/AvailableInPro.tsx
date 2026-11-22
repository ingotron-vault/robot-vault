import { PageHeader } from "./PageHeader";
import "./AvailableInPro.css";

// Where to learn about Pro. The MCP `get_edition` tool uses the same URL.
export const PRO_UPGRADE_URL = "https://ingotron.com/pro";

const DESCRIPTIONS: Record<string, string> = {
  Episodes:
    "Episodes split each recording into reviewable segments, run quality checks, and let you cut datasets from the ones you choose.",
  Datasets:
    "Datasets keep immutable membership, export state, and signed manifests for every cut you create.",
};

/** Plain text panel for a feature that is not part of the core edition. No Pro code. */
export function AvailableInPro({ feature, compact = false }: { feature: string; compact?: boolean }) {
  const body = DESCRIPTIONS[feature];
  const content = (
    <>
      <p>
        <strong>{feature}</strong> is available in Ingotron Pro. The core edition does not include it.
      </p>
      {body ? <p>{body}</p> : null}
      <p>
        <a href={PRO_UPGRADE_URL} target="_blank" rel="noreferrer">
          Learn about Ingotron Pro
        </a>
      </p>
    </>
  );
  if (compact) {
    return <div data-testid="available-in-pro" className="rv-pro-panel is-compact">{content}</div>;
  }
  return (
    <div data-testid="available-in-pro">
      <PageHeader kicker="Pro" title={`${feature} is available in Pro`} />
      <section className="rv-pro-panel" aria-label={`${feature} in Ingotron Pro`}>{content}</section>
    </div>
  );
}
