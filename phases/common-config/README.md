# Shared Robot Vault configuration

This guide is for developers and operators configuring robot storage (Phase 01)
and upload (Phase 02). The shared model covers the required `robot_id`, capture paths and retention,
upload pacing/retry bounds, server target, TLS/mTLS material, the credential
file path (`auth.token_file`), and capture metadata. JSON parsing rejects
duplicate, unknown, and non-finite values. Out-of-range values and
contradictory TLS material are errors; nothing is clamped. `tls.enabled` is the
only TLS switch. The config holds no inline token, so `VaultConfig.to_dict()`
holds no secret.

`robot_vault_config.edition` is the shared `core|pro` vocabulary for services.
`ROBOT_VAULT_EDITION` unset or empty means `core`; an unknown value fails
rather than guessing toward either exposure or feature loss.

- `read_edition()` returns the edition, or raises `ValueError` on an unknown
  value.
- `load_pro("<component>")` returns `robot_vault_pro.<component>` in edition
  `pro` and `None` in `core`. Core code reaches Pro code only through this
  call, and edition `core` never imports the Pro package. In edition `pro` it
  raises `RuntimeError` when the Pro package is not installed, or when the
  package was built for another core (its `ADAPTER_API` or `BUILT_FOR_CORE`
  differs from this core's `ADAPTER_API` or `CORE_VERSION`).
- `identity()` returns what the process runs: `edition`, `core_version`,
  `adapter_api`, `pro_version` (`null` in core) and `capabilities`.
  `python -m robot_vault_config.edition` prints it as one JSON line. The
  processor and exporter log the same values at startup, and `make release-up`
  compares them (see [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md#what-release-up-checks)).

Use `configs/vault.local.json` for the development stack and
`configs/vault.remote.json` as the separate **source-managed mTLS deployment
profile** for a robot process connecting to an independently managed ingest
server. It is not the Robots-UI enrollment path: operators add robots in the
Robots UI, then run the install command that the page shows (served by the
vault at `/robot`). Do not use `vault.remote.json` to bypass that enrollment flow. The
complete operator-facing variable and deployment contract is in
[`docs/CONFIG_REFERENCE.md`](../../docs/CONFIG_REFERENCE.md).
