/*
 * Robot onboarding and lifecycle.
 *
 * TWO DATA SOURCES, DELIBERATELY UNEQUAL
 * ======================================
 * `GET /api/v1/robots/list` is the registry the viewer role can already read;
 * it always answers and it is what makes this page useful even when
 * provisioning is unavailable. `GET /api/admin/robots/lifecycle` is the
 * query-side proxy of ingest's provisioning API: it carries state, credentials and open
 * code counts, and it requires the admin role. Older vaults may not expose
 * the proxy yet, so the page merges the two by `robot_id` and degrades per-widget
 * rather than per-page: without the proxy you still see the fleet and its upload
 * history, the lifecycle column says so in words, and every action that would
 * need the proxy is absent rather than broken.
 *
 * `/api/v1/robots/list` does not carry `state`/`auth_mode` yet either; they
 * are planned additions. The types below
 * accept them optionally so that when it does, this page starts showing robot
 * state without the proxy, with no edit here.
 *
 * NO CLIENT-SIDE ROLE GATE
 * ------------------------
 * This SPA has none, and this page does not introduce one: the server
 * is the gate (`Depends(require_role("admin"))`), and a UI that hides a control
 * based on a client-side guess about roles gets it wrong in exactly the cases
 * that matter. A 403 becomes a named sentence, which is the house pattern.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  Ban,
  KeyRound,
  Loader2,
  Plus,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Button, Callout, Modal, ToastProvider, useToast } from "@robot-vault/ui-kit";
import { http, HttpError } from "../../lib/api";
import { DEFAULT_RECORDING_ROOT, isKnownRobotState, normalizeRecordingRoot } from "../../lib/enrollment";
import {
  changeRobotState,
  createRobot,
  credentialIsLive,
  fetchRobotLifecycle,
  hasEverEnrolled,
  isEnrolledButNeverUsed,
  mintEnrollmentCode,
  ProvisioningError,
  classifyProvisioningError,
  revokeCredential,
  type MintCodeResponse,
  type RobotCredential,
  type RobotLifecycleItem,
  type RobotLifecycleResponse,
} from "../../lib/robotsAdmin";
import { PageHeader, PageStatus } from "../../components/PageHeader";
import EnrollmentCode from "./EnrollmentCode";
import UpgradeCommand from "./UpgradeCommand";
import "./robots.css";

/** 30 s: the dashboard cadence this SPA already uses for registry-shaped data. */
const FLEET_POLL_MS = 30_000;

export interface FleetRobot {
  robot_id: string;
  display_name: string | null;
  first_seen: string | null;
  last_upload_at: string | null;
  total_uploads: number;
  completed_uploads: number;
  failed_uploads: number;
  uploading: number;
  total_bytes: number;
  /** Planned additions; absent on the current route. */
  state?: string;
  auth_mode?: string;
}

interface FleetResponse {
  items: FleetRobot[];
}

export interface RobotSummary extends FleetRobot {
  topics: Array<{ topic: string; uploads: number; rows_written: number }>;
  recent_uploads: Array<{
    upload_id: string;
    original_filename: string;
    status: string;
    file_size_bytes: number;
    created_at: string;
  }>;
}

export interface MergedRobot {
  robot_id: string;
  name: string;
  fleet: FleetRobot | null;
  lifecycle: RobotLifecycleItem | null;
  /** `null` when nothing on the wire knows -- rendered as "unknown", never guessed. */
  state: string | null;
}

/**
 * One row per robot id from either source.
 *
 * A robot can exist in the registry with no lifecycle row (legacy shared-token
 * fleets) and in the lifecycle list with no uploads (freshly created, never
 * enrolled). Both must appear; dropping either would hide exactly the robots an
 * operator opened this page to fix.
 */
export function mergeRobots(
  fleet: FleetRobot[] | undefined,
  lifecycle: RobotLifecycleItem[] | undefined
): MergedRobot[] {
  const byId = new Map<string, MergedRobot>();
  for (const item of fleet ?? []) {
    byId.set(item.robot_id, {
      robot_id: item.robot_id,
      name: item.display_name || item.robot_id,
      fleet: item,
      lifecycle: null,
      state: item.state ?? null,
    });
  }
  for (const item of lifecycle ?? []) {
    const existing = byId.get(item.robot_id);
    if (existing) {
      existing.lifecycle = item;
      existing.state = item.state;
      if (item.name) existing.name = item.name;
    } else {
      byId.set(item.robot_id, {
        robot_id: item.robot_id,
        name: item.name || item.robot_id,
        fleet: null,
        lifecycle: item,
        state: item.state,
      });
    }
  }
  return [...byId.values()].sort((a, b) => a.robot_id.localeCompare(b.robot_id));
}

export default function RobotsPage() {
  // Mounted here rather than around the whole app: this
  // SPA's root has no ToastProvider and adding one changes every other page.
  return (
    <ToastProvider>
      <Robots />
    </ToastProvider>
  );
}

function Robots() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [mint, setMint] = useState<{ robotId: string; response: MintCodeResponse } | null>(null);
  const [confirming, setConfirming] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // UI_DESIGN_GUIDELINES: toasts "do not contain the only copy of an error or a
  // required next step". A failed disable or revoke is exactly that -- the
  // toast auto-dismisses and the operator is left with nothing to read.
  const [actionFailure, setActionFailure] = useState<string | null>(null);

  const fleetQ = useQuery<FleetResponse>({
    queryKey: ["robots-fleet"],
    queryFn: () => http<FleetResponse>("/api/v1/robots/list"),
    refetchInterval: FLEET_POLL_MS,
  });

  const lifecycleQ = useQuery<RobotLifecycleResponse, ProvisioningError>({
    queryKey: ["robots-lifecycle"],
    queryFn: fetchRobotLifecycle,
    refetchInterval: FLEET_POLL_MS,
    // A refused role is a settled answer; retrying it just delays the
    // sentence the operator needs to read.
    retry: (failureCount, error) => error.reason !== "forbidden" && failureCount < 1,
  });

  const provisioning = lifecycleQ.error ?? null;
  const provisioningReady = lifecycleQ.isSuccess;

  useEffect(() => {
    if (searchParams.get("add") !== "1" || !provisioningReady) return;
    setAddOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete("add");
    setSearchParams(next, { replace: true });
  }, [provisioningReady, searchParams, setSearchParams]);

  const robots = useMemo(
    () => mergeRobots(fleetQ.data?.items, lifecycleQ.data?.robots),
    [fleetQ.data, lifecycleQ.data]
  );

  const summaryQ = useQuery<RobotSummary>({
    queryKey: ["robot-summary", selected],
    queryFn: () => http<RobotSummary>(`/api/v1/robots/${encodeURIComponent(selected!)}/summary`),
    enabled: Boolean(selected),
    refetchInterval: FLEET_POLL_MS,
    retry: (failureCount, error) => (error instanceof HttpError && error.status < 500 ? false : failureCount < 1),
  });

  useEffect(() => {
    if (fleetQ.isError) {
      const status = fleetQ.error instanceof HttpError ? fleetQ.error.status : null;
      if (status === 401) toast("Unauthorized: sign in to view the fleet.", "danger");
      else if (status === 403) toast("Forbidden: the fleet list requires the viewer role.", "danger");
    }
  }, [fleetQ.isError, fleetQ.error, toast]);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["robots-fleet"] });
    void queryClient.invalidateQueries({ queryKey: ["robots-lifecycle"] });
  }

  async function runAction(key: string, action: () => Promise<string>) {
    setBusy(key);
    setActionFailure(null);
    try {
      const message = await action();
      toast(message, "success");
      invalidate();
    } catch (error) {
      const failure = classifyProvisioningError(error);
      toast(failure.message, "danger");
      setActionFailure(failure.message);
    } finally {
      setBusy(null);
    }
  }

  async function onMint(robot: MergedRobot, purpose: "enroll" | "reenroll") {
    setBusy(`mint:${robot.robot_id}`);
    setActionFailure(null);
    try {
      const response = await mintEnrollmentCode(robot.robot_id, { purpose });
      setMint({ robotId: robot.robot_id, response });
      invalidate();
    } catch (error) {
      const failure = classifyProvisioningError(error);
      toast(failure.message, "danger");
      setActionFailure(failure.message);
    } finally {
      setBusy(null);
    }
  }

  // Stable identities. `ui-kit`'s focus trap lists `onClose` in its effect
  // deps, so an inline arrow makes every parent re-render -- a 30 s fleet poll,
  // a `busy` transition -- tear the trap down and rebuild it, yanking focus out
  // of whatever the operator was typing into.
  const closeMint = useCallback(() => setMint(null), []);
  const closeAdd = useCallback(() => setAddOpen(false), []);
  const closeConfirm = useCallback(() => setConfirming(null), []);
  const handleCreated = useCallback(
    (robotId: string, response: MintCodeResponse) => {
      setAddOpen(false);
      setMint({ robotId, response });
      void queryClient.invalidateQueries({ queryKey: ["robots-fleet"] });
      void queryClient.invalidateQueries({ queryKey: ["robots-lifecycle"] });
    },
    [queryClient]
  );

  const selectedRobot = robots.find((robot) => robot.robot_id === selected) ?? null;

  return (
    <div className="rv-robots">
      <PageHeader
        kicker="System / Robots"
        title="Robots"
        description="Register a robot, hand its operator a one-time enrollment code, and control the credentials it uploads with."
        actions={
          <>
            <PageStatus>Polls every 30 seconds</PageStatus>
            <Button onClick={() => setAddOpen(true)} disabled={!provisioningReady}>
              <Plus size={15} aria-hidden="true" />
              <span>Add robot</span>
            </Button>
          </>
        }
      />

      {provisioning ? <ProvisioningNotice error={provisioning} /> : null}

      {actionFailure ? (
        <div className="rv-robots-alert rv-robots-action-failure" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>{actionFailure}</span>
          <Button size="sm" variant="secondary" onClick={() => setActionFailure(null)}>
            Dismiss
          </Button>
        </div>
      ) : null}

      <div className="rv-robots-workspace">
        <section id="robots-fleet" className="rv-robots-panel rv-robots-registry" aria-labelledby="rv-robots-fleet-title">
          <div className="rv-robots-panel__head">
            <div>
              <h2 id="rv-robots-fleet-title">Fleet</h2>
              <p className="rv-robots-panel__meta">
                Select a robot to see its credentials and recent uploads.
              </p>
            </div>
            <span className="rv-robots-count">
              {fleetQ.isSuccess || lifecycleQ.isSuccess ? `${robots.length} robots` : "—"}
            </span>
          </div>

          {fleetQ.isLoading && !lifecycleQ.isSuccess ? (
            <div className="rv-robots-state">
              <Loader2 size={17} aria-hidden="true" /> Loading the fleet…
            </div>
          ) : fleetQ.isError && !lifecycleQ.isSuccess ? (
            <div className="rv-robots-state is-error" role="alert">
              <span>
                <code>GET /api/v1/robots/list</code> did not answer. This reads the registry
                directly, so a failure here means the query service or its database is
                unreachable — not that the fleet is empty.
              </span>
              <Button size="sm" variant="secondary" onClick={() => void fleetQ.refetch()}>
                Retry
              </Button>
            </div>
          ) : robots.length === 0 ? (
            <div className="rv-robots-state">
              No robots are registered yet. Add one to mint its first enrollment code.
            </div>
          ) : (
            <div className="rv-robots-table-wrap">
              <table className="rv-robots-table">
                <caption className="rv-robots-sr">
                  Registered robots, their lifecycle state, live credentials, open enrollment codes and upload activity
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Robot</th>
                    <th scope="col">State</th>
                    <th scope="col" className="is-numeric">
                      Credentials
                    </th>
                    <th scope="col" className="is-numeric">
                      Uploads
                    </th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {robots.map((robot) => (
                    <RobotRow
                      key={robot.robot_id}
                      robot={robot}
                      selected={robot.robot_id === selected}
                      provisioningReady={provisioningReady}
                      busy={busy}
                      onSelect={() => setSelected(robot.robot_id === selected ? null : robot.robot_id)}
                      onMint={() =>
                        hasEverEnrolled(robot.lifecycle)
                          ? setConfirming({ kind: "reenroll", robot })
                          : void onMint(robot, "enroll")
                      }
                      onDisable={() =>
                        void runAction(`state:${robot.robot_id}`, async () => {
                          const result = await changeRobotState(robot.robot_id, "disable");
                          return `${robot.robot_id} is now ${result.state}. Uploads are refused and in-flight transfers are aborted.`;
                        })
                      }
                      onEnable={() =>
                        void runAction(`state:${robot.robot_id}`, async () => {
                          const result = await changeRobotState(robot.robot_id, "enable");
                          return `${robot.robot_id} was restored to ${result.state}.`;
                        })
                      }
                      onDecommission={() => setConfirming({ kind: "decommission", robot })}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section id="robots-detail" className="rv-robots-panel rv-robots-inspector" aria-labelledby="rv-robots-detail-title">
          <div className="rv-robots-inspector__head">
            <p className="rv-robots-kicker">Robot detail</p>
            <h2 id="rv-robots-detail-title" className={selectedRobot ? "is-mono" : undefined}>
              {selectedRobot ? selectedRobot.robot_id : "No robot selected"}
            </h2>
            {selectedRobot && selectedRobot.name !== selectedRobot.robot_id ? (
              <p className="rv-robots-inspector__name">{selectedRobot.name}</p>
            ) : null}
            {selectedRobot ? <RobotStateBadge state={selectedRobot.state} /> : null}
          </div>
          {!selectedRobot ? (
            <p className="rv-robots-inspector__empty">
              Select a robot in the fleet table to see its credentials and uploads.
            </p>
          ) : (
            <RobotDetail
              robot={selectedRobot}
              summary={summaryQ.data ?? null}
              loading={summaryQ.isLoading}
              error={summaryQ.isError}
              busy={busy}
              onRetry={() => void summaryQ.refetch()}
              onRevoke={(credential) => setConfirming({ kind: "revoke", robot: selectedRobot, credential })}
            />
          )}
        </section>
      </div>

      {provisioningReady ? <UpgradeCommand /> : null}

      <AddRobotDialog open={addOpen} onClose={closeAdd} onCreated={handleCreated} />

      <Modal
        open={Boolean(mint)}
        onClose={closeMint}
        title={mint ? `Enroll ${mint.robotId}` : "Enrollment code"}
        className="rv-robots-modal"
      >
        {mint ? <EnrollmentCode robotId={mint.robotId} mint={mint.response} /> : null}
        <div className="rv-robots-modal__actions">
          <Button variant="secondary" onClick={closeMint}>
            Done
          </Button>
        </div>
      </Modal>

      <ConfirmDialog
        request={confirming}
        busy={busy}
        onClose={closeConfirm}
        onConfirm={async (request) => {
          setConfirming(null);
          if (request.kind === "reenroll") {
            await onMint(request.robot, "reenroll");
            return;
          }
          if (request.kind === "decommission") {
            await runAction(`state:${request.robot.robot_id}`, async () => {
              const result = await changeRobotState(request.robot.robot_id, "decommission");
              return `${request.robot.robot_id} is ${result.state}. Every credential was revoked; its data is retained.`;
            });
            return;
          }
          await runAction(`revoke:${request.credential.credential_id}`, async () => {
            // No `at` is sent, so the immediate path is the only one
            // reachable: without that field, revocation is immediate. The
            // scheduled branch is absent rather than dead -- there is no UI for
            // choosing a time, so there is nothing to report about one.
            const result = await revokeCredential(request.robot.robot_id, request.credential.credential_id);
            return `Credential ${request.credential.fingerprint} was revoked (${result.applied}).`;
          });
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Provisioning availability
// ---------------------------------------------------------------------------

export function ProvisioningNotice({ error }: { error: ProvisioningError }) {
  if (error.reason === "forbidden") {
    return (
      <Callout tone="warning" title="Robot provisioning requires the admin role">
        You can see the fleet and its upload history. Registering robots and issuing enrollment
        codes need an administrator session.
      </Callout>
    );
  }
  return (
    <Callout tone="warning" title="Robot provisioning did not answer">
      {error.message} The fleet below still reflects the registry.
    </Callout>
  );
}

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

/**
 * The four lifecycle states, with the tone each one earns.
 *
 * `disabled` is a warning and not a danger: it records a deliberate, reversible
 * operator decision and the credentials are retained. `decommissioned` is the
 * danger, because it is terminal to the API and revoked everything.
 */
const STATE_TONE: Record<string, string> = {
  pending: "neutral",
  enrolled: "success",
  disabled: "warning",
  decommissioned: "danger",
};

export function RobotStateBadge({ state }: { state: string | null }) {
  if (!state) {
    return <StatusChip tone="neutral" label="unknown" title="No lifecycle source answered" />;
  }
  const tone = isKnownRobotState(state) ? STATE_TONE[state] : "neutral";
  return <StatusChip tone={tone} label={state} />;
}

/**
 * A tinted chip: a dot plus the state word. The word carries the meaning, so
 * the chip reads the same without its colour.
 */
function StatusChip({ tone, label, title }: { tone: string; label: string; title?: string }) {
  return (
    <span className="rv-robots-chip" data-tone={tone} title={title}>
      <i aria-hidden="true" />
      <span className="rv-robots-chip__label">{label}</span>
    </span>
  );
}

function RobotRow({
  robot,
  selected,
  provisioningReady,
  busy,
  onSelect,
  onMint,
  onDisable,
  onEnable,
  onDecommission,
}: {
  robot: MergedRobot;
  selected: boolean;
  provisioningReady: boolean;
  busy: string | null;
  onSelect: () => void;
  onMint: () => void;
  onDisable: () => void;
  onEnable: () => void;
  onDecommission: () => void;
}) {
  const now = Date.now();
  const credentials = robot.lifecycle?.credentials ?? [];
  const live = credentials.filter((credential) => credentialIsLive(credential, now));
  const stateBusy = busy === `state:${robot.robot_id}`;
  const mintBusy = busy === `mint:${robot.robot_id}`;
  const terminal = robot.state === "decommissioned";
  // A robot the provisioning API has never heard of (uploads under a legacy
  // shared credential, no lifecycle row) would 404 on every one of these.
  const actionable = provisioningReady && robot.lifecycle !== null;

  return (
    <tr data-selected={selected ? "yes" : "no"}>
      <th scope="row">
        <button
          type="button"
          className="rv-robots-link"
          onClick={onSelect}
          aria-expanded={selected}
          aria-controls="robots-detail"
        >
          <span className="is-mono">{robot.robot_id}</span>
        </button>
        {robot.name !== robot.robot_id ? <small>{robot.name}</small> : null}
      </th>
      <td>
        <RobotStateBadge state={robot.state} />
      </td>
      <td className="is-numeric">
        {robot.lifecycle ? (
          <>
            <span className="rv-robots-cell-mono">
              {live.length} live / {credentials.length}
            </span>
            <small className="rv-robots-cell-sub">
              {robot.lifecycle.open_codes} open {robot.lifecycle.open_codes === 1 ? "code" : "codes"}
            </small>
          </>
        ) : (
          <span className="rv-robots-unavailable" title="Needs the provisioning API">
            —
          </span>
        )}
      </td>
      <td className="is-numeric">
        {robot.fleet ? (
          <span className="rv-robots-cell-mono">{robot.fleet.total_uploads.toLocaleString()}</span>
        ) : (
          <span className="rv-robots-unavailable" title="No upload records">
            —
          </span>
        )}
      </td>
      <td className="rv-robots-actions-cell">
        {actionable ? (
          <div className="rv-robots-row-actions">
            <Button
              size="sm"
              variant="secondary"
              onClick={onMint}
              disabled={mintBusy || terminal}
              aria-describedby={terminal ? `rv-robots-terminal-${robot.robot_id}` : undefined}
            >
              <KeyRound size={13} aria-hidden="true" />
              <span>Enroll</span>
            </Button>
            {robot.state === "disabled" ? (
              <Button size="sm" variant="secondary" onClick={onEnable} disabled={stateBusy}>
                <RotateCcw size={13} aria-hidden="true" />
                <span>Enable</span>
              </Button>
            ) : (
              <Button
                size="sm"
                variant="secondary"
                onClick={onDisable}
                disabled={stateBusy || terminal}
                aria-describedby={terminal ? `rv-robots-terminal-${robot.robot_id}` : undefined}
              >
                <Ban size={13} aria-hidden="true" />
                <span>Disable</span>
              </Button>
            )}
            <Button
              size="sm"
              variant="danger"
              onClick={onDecommission}
              disabled={stateBusy || terminal}
              aria-describedby={terminal ? `rv-robots-terminal-${robot.robot_id}` : undefined}
            >
              <Trash2 size={13} aria-hidden="true" />
              <span>Decommission</span>
            </Button>
            {terminal ? (
              <p className="rv-robots-note" id={`rv-robots-terminal-${robot.robot_id}`}>
                Decommissioning is permanent. To use this hardware again, add it as a new robot.
              </p>
            ) : null}
          </div>
        ) : (
          <span className="rv-robots-unavailable">
            {provisioningReady ? "Not in the provisioning registry" : "Provisioning unavailable"}
          </span>
        )}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function RobotDetail({
  robot,
  summary,
  loading,
  error,
  busy,
  onRetry,
  onRevoke,
}: {
  robot: MergedRobot;
  summary: RobotSummary | null;
  loading: boolean;
  error: boolean;
  busy: string | null;
  onRetry: () => void;
  onRevoke: (credential: RobotCredential) => void;
}) {
  const now = Date.now();
  const credentials = robot.lifecycle?.credentials ?? [];
  const stranded = credentials.filter((credential) => isEnrolledButNeverUsed(credential, now));

  return (
    <div className="rv-robots-detail">
      {stranded.length ? (
        <Callout tone="warning" title="Enrolled, never uploaded">
          {stranded.length === 1 ? "A credential was" : `${stranded.length} credentials were`}{" "}
          issued more than ten minutes ago and has never been used. If the enrollment response
          was lost in flight the robot has no token: mint a replacement code, which revokes the
          unused credential in the same transaction.
        </Callout>
      ) : null}

      <div className="rv-robots-detail__grid">
        <section aria-labelledby="rv-robots-creds">
          <h3 id="rv-robots-creds">Credentials</h3>
          {!robot.lifecycle ? (
            <p className="rv-robots-note">
              Credential detail needs the provisioning API, which this vault does not expose.
            </p>
          ) : credentials.length === 0 ? (
            <p className="rv-robots-note">No credential has ever been issued for this robot.</p>
          ) : (
            <ul className="rv-robots-creds">
              {credentials.map((credential) => {
                const isLive = credentialIsLive(credential, now);
                return (
                  <li key={credential.credential_id}>
                    <div>
                      <span className="is-mono">{credential.fingerprint}</span>
                      <StatusChip
                        tone={isLive ? "success" : "neutral"}
                        label={
                          credential.revoked_at
                            ? "revoked"
                            : credential.superseded_at
                              ? "superseded"
                              : isLive
                                ? "live"
                                : "expired"
                        }
                      />
                    </div>
                    <small>
                      Issued <span className="is-mono">{credential.issued_at}</span> via{" "}
                      {credential.issued_via}
                    </small>
                    <small>
                      {credential.first_used_at ? (
                        <>
                          First used <span className="is-mono">{credential.first_used_at}</span>
                        </>
                      ) : (
                        "Never used"
                      )}
                    </small>
                    {isLive ? (
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => onRevoke(credential)}
                        disabled={busy === `revoke:${credential.credential_id}`}
                      >
                        Revoke
                      </Button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="rv-robots-note">
            Revoking one credential does not revoke its lineage. To cut a robot off entirely,
            disable the robot.
          </p>
        </section>

        <section aria-labelledby="rv-robots-uploads">
          <h3 id="rv-robots-uploads">Recent uploads</h3>
          {loading ? (
            <p className="rv-robots-note">
              <Loader2 size={14} aria-hidden="true" /> Loading upload history…
            </p>
          ) : error ? (
            <div className="rv-robots-state is-error" role="alert">
              <span>
                <code>GET /api/v1/robots/{robot.robot_id}/summary</code> did not answer.
              </span>
              <Button size="sm" variant="secondary" onClick={onRetry}>
                Retry
              </Button>
            </div>
          ) : !summary || summary.recent_uploads.length === 0 ? (
            <p className="rv-robots-note">This robot has not uploaded anything yet.</p>
          ) : (
            <ul className="rv-robots-uploads">
              {summary.recent_uploads.map((upload) => (
                <li key={upload.upload_id}>
                  <span>{upload.original_filename}</span>
                  <small className="is-mono">
                    {upload.status} · {upload.created_at}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add robot
// ---------------------------------------------------------------------------

/** `validate_robot_id` on the ingest side: <= 50 chars, no control chars, no slashes. */
export const MAX_ROBOT_ID_LENGTH = 50;

export type RobotIdProblem = "empty" | "too_long" | "unknown_sentinel" | "invalid_charset" | null;

export function robotIdProblem(value: string): RobotIdProblem {
  const trimmed = value.trim();
  if (!trimmed) return "empty";
  if (trimmed.length > MAX_ROBOT_ID_LENGTH) return "too_long";
  if (trimmed.toLowerCase() === "unknown") return "unknown_sentinel";
  if (trimmed === "." || trimmed === "..") return "invalid_charset";
  if (trimmed.includes("/") || trimmed.includes("\\")) return "invalid_charset";
  for (let index = 0; index < trimmed.length; index += 1) {
    const code = trimmed.charCodeAt(index);
    if (code < 0x20 || code === 0x7f) return "invalid_charset";
  }
  return null;
}

const ROBOT_ID_MESSAGES: Record<Exclude<RobotIdProblem, null>, string> = {
  empty: "A robot id is required.",
  too_long: `A robot id is at most ${MAX_ROBOT_ID_LENGTH} characters.`,
  unknown_sentinel: "`unknown` is reserved and cannot be a robot id.",
  invalid_charset: "A robot id cannot contain slashes or control characters.",
};

function AddRobotDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (robotId: string, mint: MintCodeResponse) => void;
}) {
  const [robotId, setRobotId] = useState("");
  const [name, setName] = useState("");
  const [recordingRoot, setRecordingRoot] = useState(DEFAULT_RECORDING_ROOT);
  const [failure, setFailure] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  useEffect(() => {
    if (!open) {
      setRobotId("");
      setName("");
      setRecordingRoot(DEFAULT_RECORDING_ROOT);
      setFailure(null);
      setWorking(false);
    }
  }, [open]);

  const problem = robotIdProblem(robotId);
  let recordingRootProblem: string | null = null;
  try {
    normalizeRecordingRoot(recordingRoot);
  } catch (error) {
    recordingRootProblem = error instanceof Error ? error.message : "The recording directory is invalid.";
  }

  async function submit() {
    if (problem || recordingRootProblem || working) return;
    setWorking(true);
    setFailure(null);
    const id = robotId.trim();
    let created = false;
    try {
      // Two calls, in this order, deliberately. "Add robot: rover-7"
      // when rover-7 is already live must NOT hand an operator a credential for
      // a running robot because of a typo -- so the create is allowed to 409
      // first, and only a genuinely new robot reaches the mint.
      await createRobot({ robot_id: id, ...(name.trim() ? { name: name.trim() } : {}) });
      created = true;
      const mint = await mintEnrollmentCode(id, {
        purpose: "enroll",
        recording_root: normalizeRecordingRoot(recordingRoot),
      });
      onCreated(id, mint);
    } catch (error) {
      const classified = classifyProvisioningError(error);
      // `created` separates two failures that look identical from here.
      // Reporting a mint failure as though nothing happened sends the operator
      // into a retry that 409s on the robot they just successfully created.
      const detail = created
        ? `${id} was created, but its enrollment code could not be minted: ${classified.message} Use the robot's Enroll action to try again.`
        : classified.httpStatus === 409
          ? `A robot called ${id} already exists. Use its Enroll action if you meant to re-enroll it.`
          : classified.message;
      setFailure(detail);
    } finally {
      setWorking(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Add robot" className="rv-robots-modal">
      <form
        className="rv-robots-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        noValidate
      >
        {failure ? (
          <div className="rv-robots-alert" role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            <span>{failure}</span>
          </div>
        ) : null}
        <div className="rv-robots-field">
          <label htmlFor="rv-robots-id">Robot id</label>
          <input
            id="rv-robots-id"
            className="rv-robots-input is-mono"
            value={robotId}
            onChange={(event) => setRobotId(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            required
            aria-describedby="rv-robots-id-hint"
          />
          <p id="rv-robots-id-hint" className="rv-robots-note">
            {robotId && problem
              ? ROBOT_ID_MESSAGES[problem]
              : "Permanent. It is the primary key uploads are filed under and cannot be renamed."}
          </p>
        </div>
        <div className="rv-robots-field">
          <label htmlFor="rv-robots-name">Display name</label>
          <input
            id="rv-robots-name"
            className="rv-robots-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoComplete="off"
            aria-describedby="rv-robots-name-hint"
          />
          <p id="rv-robots-name-hint" className="rv-robots-note">
            Optional. Defaults to the robot id.
          </p>
        </div>
        <div className="rv-robots-field">
          <label htmlFor="rv-robots-recording-root">Recording directory</label>
          <input
            id="rv-robots-recording-root"
            className="rv-robots-input is-mono"
            value={recordingRoot}
            onChange={(event) => setRecordingRoot(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            required
            aria-describedby="rv-robots-recording-root-hint"
          />
          <p id="rv-robots-recording-root-hint" className="rv-robots-note">
            {recordingRootProblem ?? "Absolute path on this robot where recordings are stored."}
          </p>
        </div>
        <div className="rv-robots-modal__actions">
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={Boolean(problem || recordingRootProblem) || working}>
            {working ? "Creating…" : "Create and mint code"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Confirmations
// ---------------------------------------------------------------------------

type ConfirmRequest =
  | { kind: "reenroll"; robot: MergedRobot }
  | { kind: "decommission"; robot: MergedRobot }
  | { kind: "revoke"; robot: MergedRobot; credential: RobotCredential };

function ConfirmDialog({
  request,
  busy,
  onClose,
  onConfirm,
}: {
  request: ConfirmRequest | null;
  busy: string | null;
  onClose: () => void;
  onConfirm: (request: ConfirmRequest) => Promise<void>;
}) {
  const [typed, setTyped] = useState("");

  useEffect(() => {
    setTyped("");
  }, [request]);

  if (!request) return <Modal open={false} onClose={onClose}>{null}</Modal>;

  // Re-enrolling an existing robot is allowed and audited, and the UI
  // requires a typed confirmation. Decommissioning earns one too: it is
  // permanent: it revokes every credential, and the hardware comes back only
  // as a new robot.
  const requiresTyping = request.kind !== "revoke";
  const confirmed = !requiresTyping || typed.trim() === request.robot.robot_id;

  const copy = {
    reenroll: {
      title: `Re-enroll ${request.robot.robot_id}`,
      body: "This robot has enrolled before. A new code issues a second credential alongside the existing ones — the right move for rebuilt hardware, the wrong one for a typo.",
      action: "Mint re-enrollment code",
      variant: "primary" as const,
    },
    decommission: {
      title: `Decommission ${request.robot.robot_id}`,
      body: "Every credential is revoked, no further codes can be minted, and uploads are refused. Recorded data is retained. Decommissioning is permanent. To use this hardware again, add it as a new robot.",
      action: "Decommission robot",
      variant: "danger" as const,
    },
    revoke: {
      title: "Revoke credential",
      body: "The credential stops authenticating immediately. Its audit trail is kept. Note that this does not revoke credentials it was rotated into — disabling the robot is the control that closes everything at once.",
      action: "Revoke credential",
      variant: "danger" as const,
    },
  }[request.kind];

  return (
    <Modal open onClose={onClose} title={copy.title} className="rv-robots-modal">
      <div className="rv-robots-confirm">
        <p className="rv-robots-copy">{copy.body}</p>
        {requiresTyping ? (
          <div className="rv-robots-field">
            <label htmlFor="rv-robots-confirm-id">
              Type <span className="is-mono">{request.robot.robot_id}</span> to confirm
            </label>
            <input
              id="rv-robots-confirm-id"
              className="rv-robots-input is-mono"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
        ) : null}
        <div className="rv-robots-modal__actions">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={copy.variant}
            disabled={!confirmed || busy !== null}
            onClick={() => void onConfirm(request)}
          >
            {copy.action}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
