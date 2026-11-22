# Visual documentation guide failure inventory

The standalone guide at `docs/understand.html` must help operators and
developers choose a maintained guide and understand the recording data flow.
These are the failures to check before adding its browser verification:

- A reader chooses the source workflow when they need the production server workflow.
- A source link is missing, points outside the intended repository, or opens a
  missing local file.
- A diagram omits an edition limit or implies that an upload is already an export.
- A control cannot be reached or used with a keyboard.
- Filtering loses relevant results or gives no explanation when there are none.
- A narrow screen clips text or requires horizontal scrolling.
- JavaScript failure hides the explanations or source links.
- External scripts, fonts, or network requests prevent offline use.

The repeatable browser check is `docs-understand.mjs` in this directory. It uses
the locked Playwright dependency already installed for the browser E2E package.
Run it from the repository root:

```sh
BROWSER_EXECUTABLE=/usr/bin/google-chrome node development/browser-e2e/docs-understand.mjs
```

It opens the real local HTML file in an isolated browser. It checks stage
disclosures, guide filters, empty results, keyboard use, mobile layout, local
link targets, and a page with JavaScript disabled. It saves screenshots, a
trace, and a JSON report under `tmp/documentation-e2e/`. Failures exit nonzero.
This checks browser behavior; a reviewer must still check technical meaning
against the linked guides.
