# Security policy

## Report a vulnerability

Report security problems privately. Do not open a public issue, pull request,
or discussion.

1. Use GitHub private vulnerability reporting: open the repository's
   **Security** tab, then **Report a vulnerability** (a GitHub security
   advisory).
2. Or email `hello@ingotron.com` with "Security" in the subject.

Include the version or image digest, the steps to reproduce, and the effect you
expect. Do not include real customer recordings or credentials.

## What to expect

- The maintainers acknowledge a report within a few business days.
- They confirm or reject the problem, and agree a fix and disclosure date with
  you.
- They publish a fix and a GitHub security advisory. They credit you if you
  want to be named.

Please give the maintainers reasonable time to fix a problem before you
disclose it.

## Scope

In scope: the code in this repository and the published `robot-vault-core-*`
images. Out of scope: your own deployment settings, third-party software that
this project only packages (report those to their projects), and the Pro
edition (report to your Pro support contact).

## Supported versions

Security fixes go to the latest release of Robot Vault core. Upgrade to the
latest release before you report a problem, if you can.

## Deploy safely

The development stack uses fixed development credentials and is for loopback
use only. Use the release topology for any real deployment. See
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).
