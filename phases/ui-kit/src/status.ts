/*
 * The tone vocabulary, and every status -> tone map the vault's surfaces share.
 *
 * WHY THE MAPS LIVE HERE
 * ======================
 * Every surface that needs colour (upload status, episode status, export
 * status, `pii_status`) takes its tone from a map in this file, because each
 * of those maps encodes a JUDGEMENT, not just a colour:
 *
 *   - `failed` is a warning and `dead_letter` is a danger, because the exporter
 *     retries the first and gives up on the second;
 *   - `excluded` is a warning and not a danger, because it records a deliberate
 *     decision rather than broken data;
 *   - `raw` PII is a warning and not a danger, because unscrubbed media is not
 *     corrupt, it just may not leave the vault.
 *
 * Those judgements have to agree wherever a status appears, and one copy per
 * page is how they stop agreeing. The pages keep their own LAYOUT (a
 * chip, a pill, a dot); the meaning is here.
 *
 * ADDING A DOMAIN
 * ---------------
 * A domain is a CLOSED vocabulary written by the backend -- a CHECK constraint
 * or an enum, cited in the comment above each map. Numeric thresholds (QA
 * scores) are policy, not vocabulary, and deliberately stay with the code that
 * owns the policy.
 */

export type Tone = "neutral" | "info" | "success" | "danger" | "warning";

/** The status vocabularies that map onto `Tone`. */
export type ToneDomain = "upload" | "episode" | "export" | "pii";

/**
 * Upload/job status (`uploads.status`, and the robot agent's in-memory job
 * status).
 */
const UPLOAD_TONE: Record<string, Tone> = {
  pending: "neutral",
  uploading: "info",
  running: "info",
  uploaded: "success",
  completed: "success",
  failed: "danger",
  cancelled: "warning",
  interrupted: "warning",
};

/*
 * `robot_vault_config.episode_status.PERSISTED_EPISODE_STATUSES`
 * (phases/common-config/src/robot_vault_config/episode_status.py), the closed
 * set of persisted episode statuses.
 *
 * `segmented` and `qa_scored` deliberately share `info`: both are "in the
 * pipeline, nothing wrong", and inventing a sixth hue for a distinction the
 * label already carries would weaken the tones that do mean something.
 * `excluded` is `warning` rather than `danger` -- it records a deliberate
 * human/QA decision to keep an episode out (and dataset cuts refuse to
 * select it), which is the `cancelled` shape above, not the `failed` one. That
 * keeps `danger` meaning exactly one thing on the Episodes page: a QA score
 * saying the data is bad.
 */
const EPISODE_TONE: Record<string, Tone> = {
  pending: "neutral",
  segmented: "info",
  qa_scored: "info",
  exported: "success",
  excluded: "warning",
};

/*
 * `export_status.status` (db/migrations/001_baseline.sql).
 *
 * `failed` is warning, not danger, and `dead_letter` is danger, not warning:
 * the exporter retries a `failed` row a bounded number of times, then writes
 * `dead_letter` and stops. One resolves itself, the other does not, and
 * colouring them the same would hide the only export state that
 * actually needs a human.
 */
const EXPORT_TONE: Record<string, Tone> = {
  pending: "neutral",
  exporting: "info",
  completed: "success",
  failed: "warning",
  dead_letter: "danger",
};

/*
 * `media_files.pii_status`, whose CHECK constraint is
 * `IN ('raw', 'scrubbed', 'not_applicable')` (db/migrations/001_baseline.sql,
 * DEFAULT 'raw'). By default the exporter refuses to export `raw`
 * media. It is `warning`, not `danger`:
 * unscrubbed media is not broken data, it is data that may not leave the vault.
 */
const PII_TONE: Record<string, Tone> = {
  raw: "warning",
  scrubbed: "success",
  not_applicable: "neutral",
};

/**
 * The upload vocabulary (an alias of `UPLOAD_TONE`). Prefer
 * `toneFor("upload", status)`; this stays exported because it is part of the
 * kit's published surface.
 */
export const STATUS_TONE: Record<string, Tone> = UPLOAD_TONE;

/** Every vocabulary, keyed by domain. */
export const TONE_BY_DOMAIN: Record<ToneDomain, Record<string, Tone>> = {
  upload: UPLOAD_TONE,
  episode: EPISODE_TONE,
  export: EXPORT_TONE,
  pii: PII_TONE,
};

/**
 * Own-property lookup, not `table[value] ?? "neutral"`.
 *
 * These keys are strings the SERVER chose, and `"constructor"` and `"toString"`
 * are perfectly legal strings that would otherwise resolve up the prototype
 * chain to a function -- which is truthy, so `??` never fires and a caller gets
 * `Object` where it expected a `Tone`. Cheap to get right, invisible to debug.
 */
function lookup(table: Record<string, Tone>, value: string): Tone {
  return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : "neutral";
}

/**
 * The tone for a status, neutral when the vocabulary does not know it.
 *
 * An unrecognised value is always `neutral` rather than an error: these
 * vocabularies can widen server-side (a new episode status, a new export
 * state) and a UI built before the widening must degrade to "no opinion", not
 * to a colour it invented or a crash.
 *
 * The one-argument form means the `upload` domain.
 */
export function toneFor(status: string): Tone;
export function toneFor(domain: ToneDomain, value: string): Tone;
export function toneFor(domainOrStatus: string, value?: string): Tone {
  if (value === undefined) return lookup(UPLOAD_TONE, domainOrStatus);
  const table = TONE_BY_DOMAIN[domainOrStatus as ToneDomain];
  return table ? lookup(table, value) : "neutral";
}

/** Whether `value` is a status the domain's vocabulary actually contains. */
export function isKnownStatus(domain: ToneDomain, value: string): boolean {
  const table = TONE_BY_DOMAIN[domain];
  return !!table && Object.prototype.hasOwnProperty.call(table, value);
}
