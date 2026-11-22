import { Link } from "react-router-dom";
import { ArrowRight, CheckCircle2, Database, Search, Upload } from "lucide-react";
import { AvailableInPro } from "../../components/AvailableInPro";
import { useIsProEdition } from "../../components/Layout";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import "./help.css";

const workflows: {
  title: string;
  body: string;
  to: string;
  action: string;
  icon: typeof Upload;
  proOnly?: boolean;
  feature?: string;
}[] = [
  {
    title: "Bring data in",
    body: "Open Intake to follow uploads from receipt through processing. A failed step stays attached to its upload so you can retry from the right place.",
    to: "/uploads",
    action: "Open Intake",
    icon: Upload,
  },
  {
    title: "Inspect a stream",
    body: "Use Explore for a robot topic and time window. Switch to SQL when you need a read-only catalog query.",
    to: "/explore",
    action: "Open Explore",
    icon: Search,
  },
  {
    title: "Review and cut",
    body: "In Pro, filter Review to the episodes you want. The cut preflight shows the current eligible and excluded counts before creation.",
    to: "/episodes",
    action: "Open Review",
    icon: CheckCircle2,
    proOnly: true,
    feature: "Episodes",
  },
  {
    title: "Verify a dataset",
    body: "Verification shows immutable membership, export state, recorded limitations, and the command for checking the manifest off the vault.",
    to: "/datasets",
    action: "Open Verification",
    icon: Database,
    proOnly: true,
    feature: "Datasets",
  },
];

export default function HelpPage() {
  const isPro = useIsProEdition();
  return (
    <div className="rv-help-page">
      <PageHeader
        kicker="Guide / Help"
        title="Operator guide"
        description="A local guide to the daily Orbit workflow. It stays available with the vault and does not open an external site."
        actions={<PageStatus>Available offline</PageStatus>}
      />

      <section id="workflows" className="rv-help-section rv-orbit-panel" aria-labelledby="help-workflows-title">
        <div className="rv-help-section__head rv-orbit-panel-heading">
          <div><small>Workflows</small><h2 id="help-workflows-title">Common workflows</h2></div>
        </div>
        <div className="rv-help-workflows-frame">
        <div className="rv-help-workflows">
          {workflows.map(({ title, body, to, action, icon: Icon, proOnly, feature }) => (
            <article key={title}>
              <span className="rv-help-workflows__icon"><Icon size={16} aria-hidden="true" /></span>
              <div>
                <h3>{title}</h3>
                {proOnly && !isPro ? (
                  <AvailableInPro feature={feature ?? title} compact />
                ) : (
                  <>
                    <p>{body}</p>
                    <Link to={to}>{action}<ArrowRight size={14} aria-hidden="true" /></Link>
                  </>
                )}
              </div>
            </article>
          ))}
        </div>
        </div>
      </section>

      <div className="rv-help-reference">
        <section id="shortcuts" className="rv-help-section rv-orbit-panel" aria-labelledby="help-shortcuts-title">
          <div className="rv-help-section__head rv-orbit-panel-heading">
            <div><small>Keyboard</small><h2 id="help-shortcuts-title">Keyboard shortcuts</h2></div>
          </div>
          <dl className="rv-help-definitions is-keys">
            <div><dt><kbd>Tab</kbd></dt><dd>Move through controls, links, chart data, and disclosures.</dd></div>
            <div><dt><kbd>Ctrl</kbd> or <kbd>Cmd</kbd> + <kbd>Enter</kbd></dt><dd>Run the current SQL query in Explore.</dd></div>
            <div><dt><kbd>Esc</kbd></dt><dd>Close an open dialog, drawer, or selected dataset detail.</dd></div>
          </dl>
        </section>

        <section id="terms" className="rv-help-section rv-orbit-panel" aria-labelledby="help-terms-title">
          <div className="rv-help-section__head rv-orbit-panel-heading">
            <div><small>Vocabulary</small><h2 id="help-terms-title">Terms used in Orbit</h2></div>
          </div>
          <dl className="rv-help-definitions">
            <div><dt>Upload</dt><dd>A source capture received by the vault.</dd></div>
            <div><dt>Episode</dt><dd>A reviewable time segment derived from an upload.</dd></div>
            <div><dt>Dataset cut</dt><dd>An immutable selection of episodes with a content-addressed manifest.</dd></div>
            <div><dt>Verification</dt><dd>An independent check of manifest identity, signature, and recorded object digests.</dd></div>
          </dl>
        </section>
      </div>
    </div>
  );
}
