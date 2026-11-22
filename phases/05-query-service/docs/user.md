# Phase 05 — Query service user guide

This guide is for operators who browse uploads, charts, and datasets in the
query service web UI or call its API.

## What this adds

- A web UI at `/ui` to browse uploads, view timeseries, and basic admin/observability.
- REST endpoints under the Query API that power the UI.

## Start the service

From the repository root, use the supported Compose wrapper:

```bash
make stack-up
```

## Use the UI

Open `http://localhost:8003/ui`. The development stack explicitly advertises
`OIDC_DEV=true`, so the single-page app (SPA) obtains its local token
automatically. A real OpenID Connect (OIDC) deployment instead shows
**Sign in** and uses Authorization Code with Proof Key for Code Exchange
(PKCE).

Current pages (full detail: `phases/05-query-service/docs/ui-dev.md`):

- Overview (`/ui/`): Postgres-backed KPI tiles (uploads in the last hour, stored bytes,
  episodes awaiting QA, and exports completed in 24 hours), edition/tenant
  chips, and a fleet table with per-robot recent uploads and processed topics
- Intake (`/ui/uploads`): filter and paginate uploads; select multiple robots and date presets;
  persist or share filters; and open a row for topics, sizes, capture context,
  and presigned S3 downloads
- Explore (`/ui/explore`): pick robot/topic/range/bucket from API-fed pickers; URL persists selection; Copy link; CSV export; SQL console with history (read-only)
- Review (`/ui/episodes`) and Verification (`/ui/datasets`) (Pro only): review
  episode QA/PII evidence, create a cut, then inspect export status, its
  datacard, Ed25519 provenance, and the offline-verification command
- Admin (`/ui/admin`): upload and processing status. There is no GC trigger in the UI — GC is the standalone
  `gc` service (`tools/s3_gc.py`), driven by config/env, not an HTTP call

The global header theme control switches every page between light and dark.
Dark is the default. The selected mode is stored locally in the browser,
persists across reloads and sign-out, and does not change server state.

## S3-first downloads

- Ensure `S3_PARQUET_BUCKET` exists. With `S3_PUBLIC_ENDPOINT` unset, the API
  returns a same-origin query download URL and the object store remains private. Set a
  public endpoint only when clients should reach the object store/a compatible proxy
  directly; `make debug-up` exposes the local object store on loopback for debugging.

## Auth (optional)

- Disabled: `AUTH_MODE=none`; the runtime endpoint reports `mode=none` and the
  SPA neither shows sign-in nor probes a development-token route.
- Dev: `AUTH_MODE=oidc` and `OIDC_DEV=true` → `/auth/dev/token`; the UI fetches
  it only after `/api/v1/auth/config` explicitly reports development mode.
- Real OIDC: set `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`,
  `OIDC_CLIENT_ID`, `OIDC_AUTHORIZATION_URL`, and `OIDC_TOKEN_URL`. The last
  three describe a public browser client: there is no client secret. Register
  `https://<vault-origin>/ui/auth/callback/` as its exact redirect URI. Optional
  inputs are `OIDC_SCOPES` (default `openid profile email offline_access`) and
  `OIDC_END_SESSION_URL`.
- Real-mode issuer and endpoint URLs must be explicit HTTPS URLs. Providers may
  use different HTTPS origins for issuer, authorization, and token endpoints;
  the CSP permits only the configured token-endpoint origin for cross-origin
  fetches. That endpoint must allow the vault web origin through CORS.
- Access and refresh tokens live only in `sessionStorage`, are refreshed once
  for concurrent callers near expiry, and disappear when the tab session or
  local sign-in session ends. Callback state is single-use and expires after
  ten minutes; return targets are restricted to `/ui` on the vault origin.
- When OIDC is enabled, all lake data, fleet, S3, dataset, admin, and
  observability API routes require at least the `viewer` role. Higher-risk
  mutations still require `operator` or `admin`. Authentication bootstrap and
  process probes remain public so login and orchestration can function.
- Sign out always clears tokens and authenticated React Query data. If
  `OIDC_END_SESSION_URL` is configured, the browser also navigates there with
  `client_id` and a fixed post-logout URI, but no token. Providers that require
  `id_token_hint` will retain their SSO session; the Robot Vault session is
  still closed, and the next sign-in may complete without prompting.
- Roles: viewer (S3 list/presign, episodes/datasets read), operator (ingest,
  task-label edits, PromQL queries on the OBS page), admin (ingest status,
  dataset-cut creation)

## API examples

```bash
RV_TOKEN=$(curl -fsS "http://localhost:8003/auth/dev/token?roles=viewer" | jq -r .access_token)
curl -fsS -H "Authorization: Bearer $RV_TOKEN" http://localhost:8003/api/robots | jq
curl -fsS -H "Authorization: Bearer $RV_TOKEN" "http://localhost:8003/api/timeseries?robot_id=demo&topic=imu&bucket=minute" | jq
# Replace UPLOAD_ID with an upload id from /api/v1/uploads/list.
KEY=$(curl -fsS -H "Authorization: Bearer $RV_TOKEN" http://localhost:8003/api/v1/uploads/UPLOAD_ID/files | jq -r '[.parquet_keys[][]][0]')
curl -fsS -H "Authorization: Bearer $RV_TOKEN" -H 'content-type: application/json' -d "{\"key\":\"$KEY\"}" http://localhost:8003/api/v1/s3/presign | jq -r .url
```

## Troubleshooting

- `/ui/obs` is the supported PromQL/ECharts observability page; there is no
  Grafana proxy or iframe. Its charts and PromQL explorer need the `operator`
  or `admin` role. A viewer sees "Operator role required".
- Postgres-backed Overview KPIs: `/api/v1/metrics/overview`
- Obs health: `/api/obs/health`
- Tenant: `/api/tenant` shows current S3 prefix scope
- Empty charts: widen the time range, try a different topic, or select a specific robot
