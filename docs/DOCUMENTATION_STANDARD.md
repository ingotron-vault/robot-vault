# Documentation standard

Use this standard when writing or reviewing Robot Vault documentation for
operators and developers. A reader should know which guide applies, what to
do, and how to check the result.

We borrow clarity principles from
[ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/about.html).
The full standard includes writing rules and a controlled dictionary. This
project uses a softer approach. We do not claim compliance or an "80%" score.

## Write for the task

- Start with the purpose, intended reader, and scope. Distinguish source
  development from a release deployment, and the core edition from Pro, when the
  distinction matters.
- State required tools, access, edition, and starting conditions before a procedure.
- Use familiar words and active verbs. Name the service or person that acts.
- Keep one main idea per sentence. Aim for about 20 words in instructions,
  but preserve technical meaning when a longer sentence is clearer.
- Use one term for one concept. Keep command names, API fields, paths,
  and UI labels exact. Define an unfamiliar term where readers first need it.
- Put one action in each numbered step. Show where to run commands, then show
  the expected result and how to recover from likely failures.
- State decision branches directly: "If the check fails, stop and inspect the
  logs." Identify who acts and which machine they use when that could be unclear.
- Put a data-loss warning or required safety condition before the affected step.
  Keep conditions such as "Pro only" and "dry-run by default" beside the action.
- Explain defaults, units, valid values, and dependencies in references.
  Use tables when readers must compare options.
- Separate supported behavior, assumptions, future work, and dated evidence.
  Link to the source of truth instead of copying a long procedure.

For example, replace "Initiate the ingestion process and subsequently ascertain
service availability" with "Start the development stack. Check that the query
service responds." Include the actual commands and expected response beside
those instructions.

## Choose the format

Formats do not form a ladder. Use the least complex one that answers the
reader's question.

| Reader's need | Format | Required support |
| --- | --- | --- |
| Follow a procedure or find an exact option | Markdown text, steps, or a table | Prerequisites, exact command, expected result |
| Understand relationships or data flow | A small diagram | Labels, direction, boundaries, and a text explanation |
| Compare choices or explore a system | Interactive HTML | Keyboard controls, mobile layout, useful default state, and source links |
| See a sequence change over time | Animation or an explainer video | Captions or transcript, pause controls, and static steps |

The [visual starting guide](understand.html) shows where each stage fits and
which document to read. It uses the reading style of the existing documentation
site. Its explanations and links work without external scripts or services.

Use Mermaid when the renderer supports it. Use a text diagram or accessible
inline SVG when offline rendering matters. Never rely on color alone to show
status, direction, or edition boundaries.

Do not add narration or motion simply to make a page feel richer. Use local or
already-authorized tools when video is useful. Paid narration services require
the user's approval for the service and cost.

## Maintain the right source

The [documentation index](README.md) identifies each topic's owner.

- Edit maintained runbooks and phase guides with the behavior they describe.
- Preserve frozen records, such as benchmark results.
  Clarify their status in the index rather than changing what they recorded.
- Change the source and rerun its generator for data cards, API snapshots,
  rendered deliverables, and other generated material.
- Keep local credentials and private operational details out of examples,
  screenshots, and external review briefs.

## Review for understanding

A link checker can detect a broken path. It cannot prove that a reader
understands a guide. Review the changed material with these questions:

1. Can an operator or developer identify the right guide and starting conditions?
2. Can the reader explain the purpose and the key terms without guessing?
3. Can the reader follow the steps in order, with exact commands and paths?
4. Can the reader recognize success, diagnose likely failure, and find recovery steps?
5. Are security conditions, data-loss risks, and edition limits still explicit?
6. Do diagrams and interactive controls agree with the text and current behavior?
7. Do claims link to code, maintained documentation, or dated evidence?
8. Can a reader who opens a deep link recover the scope and prerequisites?

Run `make docs-check` from the repository root. For a new interactive guide,
check the reader's task in a browser at desktop and mobile widths. Check keyboard
use, empty search results, local links, and the default page without JavaScript.
Save repeatable verification evidence for complex interactions. A readability
score or a sentence-length rule is a review aid, not proof of comprehension.
