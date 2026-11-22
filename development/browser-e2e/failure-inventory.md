# Browser E2E failure inventory

This inventory is for maintainers changing the browser E2E gate. The scenarios
run the real Vite applications in Chromium and control only their HTTP/API
boundary with Playwright routing. They do not import app components or add
production test hooks.

## Query console: local time and filter round-trip

Owner: the rendered Uploads page (core UI), React Router URL state, and the
browser's `datetime-local` control. Pro checks the same contract for its
Episodes page.

Observable contract: a UTC instant already in `since` is displayed as the same
wall-clock instant in the browser's time zone. Editing the input stores the
edited instant as UTC in the URL and in the corresponding API request. Clearing
the input removes `since` from the URL and the request. The Uploads table shows
the received timestamp in the browser's local zone.

Credible regressions include formatting the input in UTC in non-UTC browsers,
interpreting a local edit as UTC, leaving a stale or local-time value in the
URL/API query, retaining `since` after clearing the control, or rendering the
received timestamp in UTC. The scenario uses literal expected values for UTC,
Asia/Kolkata (its +05:30 half-hour offset catches whole-hour arithmetic), and
America/New_York so the browser and app cannot calculate the
expected result from the same faulty conversion.

Existing helper/page tests cover date formatting and URL updates with mocked
DOM/router state. They do not exercise Chromium's real time zone, native
`datetime-local` semantics, and the rendered request path together; this is the
distinct browser contract the E2E owns.

## Query console: Available in Pro panels (core UI)

Owner: the core UI build, `components/ProRoute.tsx`, `components/AvailableInPro.tsx`,
the Home tiles, the Help workflow cards and the shell footer. Scenario:
`panels.mjs`; API responses are fixtures with `edition: core`.

Observable contract: `/ui/episodes` and `/ui/datasets` show the "Available in
Pro" panel with the upgrade link, and the browser sends no request for episodes
or datasets. Home shows "Available in Pro" on the Review and Verification tiles
and asks for no dataset list. Help shows the panel for both Pro workflows. The
footer shows the version, "AGPL-3.0" and a Source link to the release tag.

Credible regressions include a core build that imports a Pro page, a route that
404s or renders blank, a core page that still calls a Pro API, and a footer
without the source link (AGPL-3.0 section 13).

## Robot console: root-scoped status and stale refresh

Owner: the real Robot UI, `RobotContext`, status banner, Files view, and
root-scoped upload availability.

Observable contract: after Robot A has a successful status, a held A status
request cannot make A's status appear for Robot B. While B's status is pending,
upload remains disabled; once B's successful status arrives, the UI reflects
B. If a later refresh for the same root fails, the last successful status stays
visible and the page clearly labels it stale.

Credible regressions include a process-global/old-query status being reused
after root selection changes, enabling upload against another robot's status,
showing no last-known data after a transient refresh failure, or displaying old
data without a stale warning. Existing component tests cover root changes and
status state with mocked API modules, but do not control an in-flight browser
request across a real selection change or exercise React Query's retained-data
error state through the rendered page.

## Test seam and side effects

No production source hooks are needed. Each test opens the real app served by
Vite, uses normal user inputs and route transitions, and intercepts API calls
at the browser network boundary. Query and robot tests use synthetic fixtures
and never call a real vault backend.
