# Query UI developer guide

> **Design contract:** [`docs/UI_DESIGN_GUIDELINES.md`](../../../docs/UI_DESIGN_GUIDELINES.md)
> is normative for the Phase-05 Orbit management SPA. This file documents the
> implemented routes, data, security, and developer workflow; it does not
> define a separate visual system.

This guide describes the implemented query service UI at `/ui` for developers.
The UI has two builds: the **core** build (AGPL-3.0, no Pro code) and the
**Pro** build. See "Core and Pro builds" below. It covers fleet and upload
browsing, timeseries, downloads, observability, and admin tools. OpenID Connect (OIDC) login with Proof Key for
Code Exchange (PKCE), role-based access control (RBAC), rate limits, Content
Security Policy (CSP), HTTP Strict Transport Security (HSTS), and audit logging
are implemented.

### What is implemented now (current)
- UI shell and navigation under `/ui` (React SPA), including a global
  light/dark theme control. Dark is the default; an explicit choice persists
  under `localStorage["robot-vault.theme"]`, survives sign-out, and applies to
  every route rather than only Explore.
- Overview (`/ui/`, implemented in `pages/Home/index.tsx`): the operator's
  landing page. An edition
  chip (`GET /api/v1/edition`) and a tenant-scope chip (`GET /api/tenant`);
  four Postgres-backed KPI tiles (`uploads_last_hour`, `storage_bytes_total`,
  `episodes_awaiting_qa`, and `exports_completed_24h` from
  `GET /api/v1/metrics/overview`) that each degrade independently to "—" when
  that registry is unavailable, plus a page-level retry banner when every KPI
  is unavailable or the endpoint itself failed; quick links to
  Uploads (1h/24h) and Explore; and a Fleet table (`GET /api/v1/robots/list`)
  — one row per robot with last-upload/uploads/completed/failed/in-flight/
  stored, expandable to that robot's recent uploads and processed topics
  (`GET /api/v1/robots/{id}/summary`). The Review and Verification tiles link
  to their routes. In the core build they say "Available in Pro". Pro adds
  dataset export recovery rows.
  The shell footer shows the version (`core_version` from `GET /api/v1/edition`
  when the server sends it, else the build-time `VITE_RV_VERSION`), the
  "AGPL-3.0" licence and a Source link to the release tag (AGPL section 13).
- Intake (`/ui/uploads`, implemented under `pages/Uploads/`): paginated list with filters (multi-select robots, status, time presets, search), sortable columns, **auto-refresh always enabled (10s)**, URL persistence and copyable links, saved views, detail drawer with per-topic stats, presigned S3 downloads, **live progress tracking**, CSV export. The detail drawer also renders the capture-context fields when present on the upload — operator, site, permission ref, task label, session tags — sourced from `uploads.operator/site/permission_ref/session_tags/task_label`.
- Review (`/ui/episodes`) and Verification (`/ui/datasets`): Pro adds these
  pages (see the Pro documentation). In the core build both routes show the
  "Available in Pro" panel.
- Explore: robot and topic selection (API-fed), URL persistence and copyable links, auto bucket selection, zoom/pan with brush and reset, timeseries panel, CSV export, SQL console (read-only) with history
- Observability (`/ui/obs`, `pages/Obs/index.tsx`): a purpose-built
  PromQL/ECharts page (uploads-completed rate, HTTP request rate by path,
  average query duration, ad-hoc PromQL with CSV export, and active alerts) fed
  by `GET /api/obs/promql/range` and `GET /api/obs/alerts`. There is no Grafana
  proxy or iframe. PromQL needs the `operator` or `admin` role; for a viewer
  the page shows "Operator role required" and does not retry.
- Admin: ingest status (GC is the `gc` service only — see below)
- S3-first: `/api/v1/s3/list` and `/api/v1/s3/presign` endpoints with `S3_PREFIX_SCOPE` and public URL rewriting

Intake's automatic S3 discovery has a bounded scan and presents a storage or
limit failure as an error rather than an empty collection. The Intake
filter handlers patch related URL parameters in one update;
changing a filter and resetting its page offset cannot discard the new filter
or unrelated share-link parameters.
- Security: real OIDC Authorization Code + S256 PKCE, explicitly advertised
  dev-token mode, RBAC guards (viewer/operator/admin), Redis token bucket,
  CSP/HSTS, and audit logs
- `@robot-vault/ui-kit` adoption: the SPA now consumes the shared workspace package (`Button`, `Card`, `Table`, `KpiTile` (which composes the kit `Stat` and `TrendSpark`), `StatusPill`, `Toast`/`ToastProvider`, the Tailwind preset and `tokens.css`) instead of one-off local styling. `StatusPill`/`Toast` and the new `success`/`warning`/`danger` foreground tokens come from the kit, which holds contrast of at least 4.5:1 in both themes.

### UX principles
- Clean, modern, responsive, and governed by the paired light/dark Orbit
  design contract
- Fast feedback; optimistic updates where safe
- S3-first data path; zero temp files on server
- Minimal clicks from robot → upload → detail → download/visualize

### Technology choices
- Server: FastAPI (existing `query` service)
- Frontend: React SPA with Vite build (TypeScript)
- Styling: TailwindCSS with self-hosted Instrument Sans (variable) and IBM Plex
  Mono fonts
- Charts: ECharts (via echarts-for-react)
- Observability: custom PromQL/ECharts page at `/ui/obs`; Prometheus backs that
  page's operational charts, while the Overview KPIs come from Postgres

### Core and Pro builds

This source builds the core UI. Its `src/pro/index.ts` is a stub
(`installed = false`, no routes, no `HomeAttention`), so `/ui/episodes` and
`/ui/datasets` show the "Available in Pro" panel (`components/AvailableInPro.tsx`).
The Pro edition builds the same source with its own `src/pro/` module; that
build is documented with the Pro edition.

- `useIsProEdition()` (in `components/Layout.tsx`) is true only when the server
  reports `edition === "pro"` **and** the build has `pro.installed`. The Pro nav
  links, the tour steps and the Home and Help Pro content follow it. While the edition
  is loading, or if the request fails, the answer is false.
- `components/ProRoute.tsx` picks the Pro page or the panel for each Pro route.
  A Pro build waits for the edition answer before it chooses.
- The contract between core and Pro is `src/lib/proContract.ts` (`ProModule`,
  `HomeAttentionState`).
- Core code never imports a Pro page.

Build and test the core UI (from the repository root, after `npm ci`):

```sh
npm run typecheck --workspace robot-vault-ui
npm test --workspace robot-vault-ui
npm run build --workspace robot-vault-ui
```

### Directory layout (query service)
- `phases/05-query-service/src/ui-spa/` React SPA source (TypeScript), core build
- `phases/05-query-service/src/query_service/main.py` FastAPI routes and API assembly
- Build output: Vite compiles SPA to `/app/src/ui` in container

### Core pages and navigation
- Primary nav (`Layout.tsx`): Overview | Intake | Explore | **Review** | **Verification**
  - Route mapping remains Overview → `/ui/`, Intake → `/ui/uploads`, Explore →
    `/ui/explore`, Review → `/ui/episodes`, and Verification → `/ui/datasets`.
  - Admin and the availability-gated OBS link are quieter system utilities in
    the same shell rather than primary workflow steps.
  - **Review and Verification are Pro features**:
    `Layout.tsx` shows both nav links only when `useIsProEdition()` is true (see
    "Core and Pro builds"). The routes always exist. Without Pro they show the
    "Available in Pro" panel, with a plain-text description and the upgrade link
    `https://ingotron.com/pro`.
  - Contextual utilities include Copy link, Saved views, the global theme
    control, Help, and authentication.

### APIs used

Every data-bearing route below sits behind the router-wide viewer floor when
`AUTH_MODE=oidc` (see "Security and auth" below); per-route notes call out
only the additional operator/admin requirements.

- `GET /api/robots`
- `GET /api/uploads/summary`
- `GET /api/timeseries`
- `GET /api/v1/message-types`
- `GET /api/v1/uploads/list`
- `GET /api/v1/uploads/{id}/detail`, `GET /api/v1/uploads/{id}/files`
- Pro adds the episode and dataset routes (see the Pro documentation)
- `GET /api/v1/robots/list`, `GET /api/v1/robots/{id}/summary` (Overview's fleet table)
- `GET /api/v1/edition` (open; drives the Review/Verification nav gate,
  Overview's edition chip and the footer version)
- `GET /api/v1/auth/config` (open, no-store; drives none/dev/real OIDC bootstrap)
- `GET /api/v1/s3/list`, `POST /api/v1/s3/presign`
- `GET /api/v1/metrics/overview` (Postgres-backed Overview KPI tiles)
- `GET /api/tenant`, `GET /api/obs/health`
- Admin: `GET /api/admin/ingest/status`
  - There is deliberately no GC endpoint. GC is the `gc` service
    (`tools/s3_gc.py`) only — see docs/CONFIG_REFERENCE.md "GC service
    (mark-and-sweep)".

### Security and auth
- `AUTH_MODE=oidc` enables a viewer floor on every data-bearing router;
  operator/admin dependencies remain additive. Only auth/bootstrap and
  explicit process probes are public.
- `GET /api/v1/auth/config` is the SPA's open, no-store bootstrap. It reports
  `none`, `dev`, or a redacted real-OIDC public-client contract. The SPA calls
  `/auth/dev/token` only after the backend explicitly reports `dev`; merely
  receiving a 404 or finding no stored token can never enable development auth.
- Real mode requires `OIDC_CLIENT_ID`, `OIDC_AUTHORIZATION_URL`, and
  `OIDC_TOKEN_URL` in addition to the issuer/audience/JWKS inputs used by API
  verification. It is a public client with no secret. Optional
  `OIDC_SCOPES` defaults to `openid profile email offline_access`; optional
  `OIDC_END_SESSION_URL` supports token-free front-channel logout for providers
  that accept `client_id` plus `post_logout_redirect_uri`.
- Register the exact external callback `https://<vault-origin>/ui/auth/callback/`.
  The SPA validates single-use state, uses S256 PKCE, rejects callback return
  targets outside `/ui`, stores real tokens in `sessionStorage`, and
  single-flights refresh. Authenticated API requests are same-origin only;
  cross-origin codes/refresh tokens go only to the configured HTTPS token URL,
  whose origin is the sole IdP addition to CSP `connect-src`.
- Sign out always clears the Robot Vault token session and React Query cache.
  No `id_token_hint` is retained or sent: if an IdP requires that hint for
  federated logout, its SSO cookie remains and a later sign-in may not prompt.
- Roles: viewer (S3 list/presign and read routes), operator (ingest status),
  admin (ingest status); Pro adds role checks for its own routes
- CSP uses self-hosted scripts without `unsafe-inline`; optional HSTS, Redis
  rate limiter with per-path limits, audit logs

### Developer quick start
- Compose: `docker compose up -d objectstore objectstore-access-ready postgres redis ingest processor query`
  (or just `make stack-up`, which also builds, migrates and waits for health)
- UI: `http://localhost:8003/ui`
- Auth: nothing to click in the dev compose default because the backend
  explicitly reports `OIDC_DEV=true`. Real OIDC shows Sign in/Sign out in the
  global header — see "Security and auth" above.
- Test S3: ensure the bucket exists. Leave `S3_PUBLIC_ENDPOINT` unset for the
  default same-origin query download proxy. Set it only when intentionally
  exposing the object store/a compatible proxy directly to the browser (for local debug,
  `make debug-up` binds the object store to loopback).

### Testing
- The query SPA uses Vitest + React Testing Library (`npm test --workspace
  robot-vault-ui`) and strict TypeScript (`npm run typecheck --workspace
  robot-vault-ui`). Core suites cover Overview, Intake, layout gates, the Pro stub
  (`ProStub.core-only.test.tsx`) and status rendering. The Pro suites run with the
  Pro build.
- Browser checks: `development/browser-e2e` runs the core UI (Intake time zones,
  the "Available in Pro" routes, Home, Help and the footer).
- `auth.test.ts` pins PKCE parameters, callback-state rejection, storage
  failure, refresh single-flight, network deadlines and origin boundaries;
  `AuthControl.test.tsx` pins the real sign-in/sign-out and explicit-dev UX.
- Theme coverage pins the dark default, light/dark persistence, invalid or
  unavailable-storage fallback, root class and `color-scheme` application,
  sign-out preservation, accessible control labels, and theme-aware charts.
- `npm run build --workspace robot-vault-ui` exercises the same Vite build the
  phase-05 Dockerfile ships, including the deep-link callback HTML.
- Backend routes stay covered by `phases/05-query-service/tests/unit` (FastAPI `TestClient` +
  monkeypatched `psycopg`), which is the real regression net for anything the SPA calls.

### Next ideas
- Better KPI drill-downs and links
- Tenant switcher UI (when multi-tenant enabled)
- Hardening: CSP nonces, cookie-based session when upstream IdP is used
- Additional page-level accessibility and browser integration coverage
