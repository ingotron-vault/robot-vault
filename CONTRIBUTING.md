# Contributing to Robot Vault

Thank you for your interest in Robot Vault. This page explains how to report
problems, propose changes, and prepare a pull request.

## Outside contributions are not merged yet

**We do not merge outside code until the contributor license agreement (CLA)
is in force.** The draft is in [`CLA.md`](CLA.md) and is still under legal
review. Until it is final:

- You can open issues and discussions. They are welcome.
- You can open a pull request to show an idea. A maintainer reads it, but does
  not merge it.
- We do not use the Developer Certificate of Origin (DCO) as a substitute.

Why: the maintainers also ship this code in a paid Pro edition under a
commercial license. The CLA is what lets them do that for outside code. When
the CLA is live, the pull request check asks you to sign it once, and the
maintainers keep the signing records.

## Report a problem

- Bug or feature request: use the issue templates on GitHub.
- Security problem: do **not** open an issue. Follow [`SECURITY.md`](SECURITY.md).
- Behavior of the Pro edition: ask your Pro support contact. This repository
  does not contain Pro code.

## Prepare a change

1. Fork the repository and create a branch.
2. Keep one change per pull request: one behavior, its closest test, and its
   documentation.
3. Put unit contracts beside the code they test. Prefer end-to-end tests for
   whole-pipeline behavior. Do not add tests after the fact to hide a gap.
4. Update the authoritative document for the topic in the same pull request.
   [`docs/README.md`](docs/README.md) names the owner of each topic. Follow the
   [documentation standard](docs/DOCUMENTATION_STANDARD.md).
5. Run the checks that match your change:

   ```bash
   make docs-check
   make test-unit
   make ui-test
   ```

   Add `make test-rust`, `make test-e2e`, or the release gates when your change
   touches those contracts. [`development/README.md`](development/README.md)
   describes the workflow and [`docs/TESTING.md`](docs/TESTING.md) the scope
   of each suite.
6. Open the pull request and fill in the template.

## What happens next

A maintainer reviews your pull request. Maintainers apply an accepted change
upstream, and it arrives in this repository in a later release. So the commit
you see here can differ from the commit you wrote.

## Conduct and license

Everyone who takes part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
The code is licensed under AGPL-3.0-only (see [`LICENSE`](LICENSE)). The names
and logos are covered by [`TRADEMARKS.md`](TRADEMARKS.md).
