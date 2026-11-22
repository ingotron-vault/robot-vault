import { AvailableInPro } from "./AvailableInPro";
import { useEditionQuery, useIsProEdition } from "./Layout";
import { pro } from "../pro";

/** The Pro page when this build and the server are both Pro, else the "Available in Pro" panel. */
export function ProRoute({ route, feature }: { route: "episodes" | "datasets"; feature: string }) {
  const editionQ = useEditionQuery();
  const isPro = useIsProEdition();
  const Page = pro.routes[route];
  // A Pro build waits for the edition answer instead of flashing the panel.
  if (pro.installed && editionQ.isLoading) {
    return <div className="p-4 text-sm text-muted">Loading…</div>;
  }
  return isPro && Page ? <Page /> : <AvailableInPro feature={feature} />;
}
