# Documentation

This directory contains Robot Vault's maintained documentation. The root
[`README.md`](../README.md) is the entry point; this page tells you which
document owns each subject. These documents describe the core edition. Parts
that need the paid Pro edition are labelled "(Pro)".

New here? Open the [visual starting guide](understand.html) to follow a recording
through the system or find a guide for your task. It covers both operators and
developers and works offline. Use the [documentation standard](DOCUMENTATION_STANDARD.md)
when writing or reviewing current docs.

## Operator documentation

| Need | Source of truth |
| --- | --- |
| Run the core walkthrough | [`USER_GUIDE.md`](USER_GUIDE.md) |
| Configure services and feature gates | [`CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md) |
| Back up, restore, upgrade, or operate the release deployment | [`OPERATIONS.md`](OPERATIONS.md) |
| Run automated and manual verification | [`TESTING.md`](TESTING.md) |
| Generate and load the example bundle | [`EXAMPLES.md`](EXAMPLES.md) |
| Start a reduced service set manually | [`reference/COMPOSE_MINIMAL.md`](reference/COMPOSE_MINIMAL.md) |

## Engineering documentation

| Subject | Source of truth |
| --- | --- |
| System boundaries, data flow, and security model | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Database tables and ordered migrations | [`DB_SCHEMA.md`](DB_SCHEMA.md) and [`../db/migrations/`](../db/migrations/) |
| Orbit visual and interaction contract | [`UI_DESIGN_GUIDELINES.md`](UI_DESIGN_GUIDELINES.md) |
| DEFECT/RISK checks and calibration (Pro) | Part of the Pro edition documentation |
| Development workflow and conventions | [`../development/README.md`](../development/README.md) |
| Writing, visual explanations, and reader review | [`DOCUMENTATION_STANDARD.md`](DOCUMENTATION_STANDARD.md) |
| Local PC build, test, and deployment workflow | [`../README.md`](../README.md#quick-start) and [`../development/README.md`](../development/README.md#supported-local-loop) |
| Completed changes | [`../CHANGELOG.md`](../CHANGELOG.md) |

Implementation details live beside the owning code:

- [`../phases/01-robot-storage/docs/`](../phases/01-robot-storage/docs/) — local
  recording inventory, manifests, hashing, and retention
- [`../phases/02-robot-upload-agent/docs/`](../phases/02-robot-upload-agent/docs/) —
  queueing, upload policy, retry, and robot-side operation
- [`../phases/03-ingest-server-rs/docs/`](../phases/03-ingest-server-rs/docs/) —
  gRPC ingest, authentication, storage, and admin endpoints
- [`../phases/04-processing-pipeline/docs/`](../phases/04-processing-pipeline/docs/) —
  MCAP processing, typed Parquet, episodes, media, and QA
- [`../phases/05-query-service/docs/`](../phases/05-query-service/docs/) — query
  API, authorization, UI operation, and SPA development

## Evidence and published material

These files have a different lifecycle from runbooks:

- [`../examples/`](../examples/) contains generated sample data cards and asset
  provenance.
- Raw benchmark evidence is kept outside the core repository. Performance
  claims must retain their workload and host qualifications.

Generated PDF, HTML, JSON, YAML, and compressed evidence is kept only when it
is an intentional offline or review artifact. Do not hand-edit generated
outputs; update their source and run the documented generator.

## Maintenance rules

1. Keep one current source of truth for each topic. Link to it instead of
   copying configuration tables, command sequences, or status lists.
2. Track open work in issues, completed work in
   [`../CHANGELOG.md`](../CHANGELOG.md), and current behavior in the relevant
   runbook or phase document. Do not commit session handovers or completed
   execution plans as permanent docs.
3. Avoid undated test counts, performance figures, port lists, and migration
   totals. When a number is evidence, include its date, workload, and scope.
4. Change docs in the same commit as the behavior they describe. If code,
   migrations, generated API schemas, and prose disagree, resolve the mismatch
   rather than documenting both versions.
5. Use relative links for repository content and descriptive link text.
   Apply the [documentation standard](DOCUMENTATION_STANDARD.md) to current
   guides. Preserve exact commands and technical conditions when simplifying prose.
6. Run the documentation gate from the repository root:

   ```bash
   make docs-check
   ```

The gate checks repository Markdown structure, local links and anchors, fenced
code blocks, and final newlines, including unignored files that have not yet
been added to Git. Product changes should also run the relevant test suites
from [`TESTING.md`](TESTING.md).
