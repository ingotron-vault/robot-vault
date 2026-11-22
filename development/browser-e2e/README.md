# Browser E2E gate

Use this gate when changing the Query Console (core UI) or the Robot Console.
It runs their Vite applications in Chromium. Pro has its own browser
gate. Playwright controls API responses at
the browser network boundary. Each scenario still uses the rendered page,
normal browser controls, URL changes, and real application requests.

Run the supported gate from the repository root:

```sh
make test-browser
```

The gate uses Playwright 1.63.0 and the pinned `mcr.microsoft.com/playwright:v1.63.0-noble`
image. For host-side runs, install the app dependencies and this package's
locked Playwright dependency, then set `BROWSER_EXECUTABLE` to an installed
Chromium-compatible browser path:

```sh
npm ci
npm ci --prefix development/browser-e2e
BROWSER_EXECUTABLE=/usr/bin/google-chrome npm test --prefix development/browser-e2e
```

`BROWSER_APPS` selects the applications, comma-separated: `query` and
`robot` (the default, and what `make test-browser` passes). Only the
selected applications start, and only their scenario modules load. An unknown
name fails the run, and so does a selected application that ran no checks.

The runner starts a separate Vite process for each application on an available
strict port, records per-app server logs, and stops only the processes it owns.
A missing browser or failed assertion exits nonzero; scenarios never silently
skip. `BROWSER_SOURCE_ROOT` can point to a repository-shaped temporary checkout
to compare an alternate source baseline; that checkout needs the corresponding
app dependencies installed.

Reports, screenshots, Playwright traces, Vite logs, and `report.json` are saved
under `BROWSER_ARTIFACT_DIR`, defaulting to the repository's `tmp/browser-e2e/`.
The report records each assertion and observed API request, as well as browser
errors and artifact paths. [`failure-inventory.md`](failure-inventory.md)
describes the observable contract and regressions owned by each scenario.
