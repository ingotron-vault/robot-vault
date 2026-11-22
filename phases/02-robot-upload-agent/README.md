# Robot upload agent (Phase 02)

Use the command-line interface (CLI) or web UI to upload recorded files on
demand. The agent supports whole-file retry and durable multipart resume,
bounded concurrency, shared
per-queue pacing, integrity checks and stable-file gating. Expected preparation
errors remain attached to their file while healthy queued files continue; the
CLI returns a failing exit status when selected work fails. There is no
background scheduler or SSID upload gate. An `.mcap` file is uploaded only when
it ends with a complete MCAP footer.

The browser UI is the Vite single-page app (SPA) in `src/ui-spa`. Build it from
the repository root with `make ui-build` before starting the `robot` web service. Generated
assets are intentionally not committed; if they are absent, `/` returns an
explicit 503 while the API remains available. There is no legacy JavaScript UI
fallback.

Start with the [user guide](docs/user.md) or the more detailed
[usage guide](docs/usage.md). For implementation details, see the
[technical guide](docs/technical.md). To set up and test the agent, see
[run and test](docs/run-and-test.md).
