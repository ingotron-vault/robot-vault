import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button, toneFor, type Tone } from "@robot-vault/ui-kit";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import { http, HttpError } from "../../lib/api";
import "./admin.css";

interface IngestStatus {
  uploads_counts?: Record<string, number>;
  processing_counts?: Record<string, number>;
  processed_count?: number;
  robots_count?: number;
  recent_uploads?: Array<{
    upload_id: string;
    robot_id: string;
    filename: string;
    status: string;
    started_at?: string | null;
    completed_at?: string | null;
  }>;
}

// GET /api/admin/ingest/status now always raises HTTPException on failure
// (503 db_not_configured / database_unavailable) instead of returning HTTP
// 200 with a `{error}` body, so `http()` rejects with a typed `HttpError`
// carrying the real backend detail. Surface that detail on the operator's
// outage screen instead of a one-size-fits-all message -- this page is the
// tool for diagnosing a real outage, and the specific reason has real
// debugging value that a generic string throws away.
function ingestErrorDetail(error: unknown): string | undefined {
  return error instanceof HttpError && typeof error.detail === "string" && error.detail.trim()
    ? error.detail
    : undefined;
}

export default function Admin() {
  const ingest = useQuery<IngestStatus>({
    queryKey: ["admin-ingest"],
    queryFn: () => http<IngestStatus>(`/api/admin/ingest/status`),
    refetchInterval: 30_000,
  });

  // NOTE: this page used to carry a "GC" panel wired to
  // POST /api/admin/gc/dry-run and POST /api/admin/gc/execute. Those
  // endpoints were a second, drifted copy of the garbage collector and
  // have been deleted (see query_service/main.py for the full rationale):
  // their reachable set was always empty for the default
  // `processed/parquet/` prefix, so "Execute" would have swept every live
  // Parquet file. GC is the `gc` service (tools/s3_gc.py) only — driven by
  // configs/gc.yaml + GC_* env, observed via its Prometheus metrics — and
  // deliberately has no HTTP trigger.

  const uploadCounts = Object.entries(ingest.data?.uploads_counts || {});
  const processingCounts = Object.entries(ingest.data?.processing_counts || {});
  const uploadTotal = uploadCounts.reduce((total, [, count]) => total + count, 0);
  const processingTotal = processingCounts.reduce((total, [, count]) => total + count, 0);
  const recentUploads = ingest.data?.recent_uploads || [];

  return (
    <div className="rv-admin">
      <PageHeader
        kicker="System / Admin"
        title="Ingest administration"
        description="Operational visibility for uploads, processing workers, and recently completed captures."
        actions={<PageStatus>Refreshes every 30 seconds</PageStatus>}
      />

      {(ingest.isLoading || ingest.isError) ? (
        <section id="admin-overview" className="rv-admin-panel rv-orbit-panel" aria-labelledby="rv-admin-ingest-title">
          <PanelHead id="rv-admin-ingest-title" kicker="Pipeline status" title="Ingest overview" />
          {ingest.isLoading ? (
            <div className="rv-admin-state"><Loader2 className="rv-admin-spin" size={16} aria-hidden="true" /> Loading ingest status…</div>
          ) : (
            <div className="rv-admin-state is-error" role="alert">
              <span>{ingestErrorDetail(ingest.error) ?? "Ingest status unavailable. Check query-service connectivity and retry."}</span>
              <Button size="sm" variant="secondary" onClick={() => { void ingest.refetch(); }}>Retry</Button>
            </div>
          )}
        </section>
      ) : (
        <>
          <section id="admin-overview" className="rv-admin-panel rv-orbit-panel" aria-labelledby="rv-admin-ingest-title">
            <PanelHead id="rv-admin-ingest-title" kicker="Pipeline status" title="Ingest overview" />
            <div className="rv-admin-stats-frame">
              <dl className="rv-admin-stats">
                <div><dt>Uploads</dt><dd>{uploadTotal.toLocaleString()}</dd><small>across {plural(uploadCounts.length, "state", "states")}</small></div>
                <div><dt>Processing</dt><dd>{processingTotal.toLocaleString()}</dd><small>active worker records</small></div>
                <div><dt>Processed</dt><dd>{(ingest.data?.processed_count ?? 0).toLocaleString()}</dd><small>available for exploration</small></div>
                <div><dt>Robots</dt><dd>{(ingest.data?.robots_count ?? 0).toLocaleString()}</dd><small>registered fleet</small></div>
                <div><dt>Recent</dt><dd>{recentUploads.length.toLocaleString()}</dd><small>latest upload records</small></div>
              </dl>
            </div>
          </section>

          <div className="rv-admin-workspace">
            <section id="admin-activity" className="rv-admin-panel rv-admin-recent rv-orbit-panel" aria-labelledby="rv-admin-recent-title">
              <PanelHead
                id="rv-admin-recent-title"
                kicker="Activity"
                title="Recent uploads"
                note={plural(recentUploads.length, "record", "records")}
              />
              {recentUploads.length ? (
                <div className="rv-admin-table-wrap">
                  <table className="rv-admin-table">
                    <thead>
                      <tr>
                        <th scope="col">Capture</th>
                        <th scope="col" className="is-robot">Robot</th>
                        <th scope="col" className="is-time">Time (UTC)</th>
                        <th scope="col">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recentUploads.map((record, idx) => {
                        const time = record.completed_at || record.started_at || "time unavailable";
                        return (
                          <tr key={record.upload_id || idx}>
                            <td>
                              <div className="rv-admin-capture">
                                <b title={record.filename}>{record.filename}</b>
                                <small title={record.upload_id}>
                                  <span className="rv-admin-inline is-robot">{record.robot_id} · </span>
                                  {record.upload_id}
                                </small>
                                <small className="rv-admin-inline is-time">{time}</small>
                              </div>
                            </td>
                            <td className="is-robot">{record.robot_id}</td>
                            <td className="is-time is-mono">{time}</td>
                            <td><span className={`rv-admin-chip is-${toneFor("upload", record.status)}`}><i aria-hidden="true" />{record.status}</span></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="rv-admin-empty">
                  <p>No recent uploads.</p>
                  <small>Uploads appear here as soon as a robot starts sending a capture.</small>
                </div>
              )}
            </section>

            <section id="admin-states" className="rv-admin-panel rv-admin-distribution rv-orbit-panel" aria-labelledby="rv-admin-states-title">
              <PanelHead id="rv-admin-states-title" kicker="Pipeline distribution" title="State distribution" />
              <div className="rv-admin-breakdowns">
                <StateGroup id="rv-admin-upload-counts" title="Upload states" counts={uploadCounts} total={uploadTotal} tone={(state) => toneFor("upload", state)} empty="No upload counts reported." />
                <StateGroup id="rv-admin-processing-counts" title="Processing states" counts={processingCounts} total={processingTotal} tone={processingTone} empty="No processing counts reported." />
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}

/*
 * `processing_status.status` (db/migrations/001_baseline.sql CHECK:
 * pending, processing, completed, completed_empty, failed, dead_letter). The
 * kit has no processing domain, so this keeps the same judgements as Intake:
 * a completed run with no rows needs a look, a dead letter needs a human.
 */
const PROCESSING_TONE: Record<string, Tone> = {
  pending: "neutral",
  processing: "info",
  completed: "success",
  completed_empty: "warning",
  failed: "danger",
  dead_letter: "danger",
};

function processingTone(state: string): Tone {
  return Object.prototype.hasOwnProperty.call(PROCESSING_TONE, state) ? PROCESSING_TONE[state] : "neutral";
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

function PanelHead({ id, kicker, title, note }: { id: string; kicker: string; title: string; note?: string }) {
  return (
    <div className="rv-admin-panel-heading rv-orbit-panel-heading">
      <div><small>{kicker}</small><h2 id={id}>{title}</h2></div>
      {note ? <span className="rv-admin-panel-note">{note}</span> : null}
    </div>
  );
}

function StateGroup({
  id,
  title,
  counts,
  total,
  tone,
  empty,
}: {
  id: string;
  title: string;
  counts: Array<[string, number]>;
  total: number;
  tone: (state: string) => Tone;
  empty: string;
}) {
  return (
    <section className="rv-admin-states" aria-labelledby={id}>
      <div className="rv-admin-states__head">
        <h3 id={id}>{title}</h3>
        {counts.length ? <span>{total.toLocaleString()} total</span> : null}
      </div>
      {counts.length ? (
        <ul>
          {counts.map(([state, count]) => {
            const share = total > 0 ? (count / total) * 100 : 0;
            return (
              <li key={state} className={`is-${tone(state)}`}>
                <b>{state.replace(/_/g, " ")}</b>
                <span className="rv-admin-states__bar" aria-hidden="true"><span style={{ width: `${Math.max(share, count > 0 ? 1.5 : 0)}%` }} /></span>
                <em>{count.toLocaleString()}</em>
              </li>
            );
          })}
        </ul>
      ) : (
        <p>{empty}</p>
      )}
    </section>
  );
}
