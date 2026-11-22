# Robot Vault Orbit — UI design guidelines

Use this guide when designing, implementing, or reviewing the Orbit management
interface. It is for product designers and frontend developers.

> **Status: maintained design contract.** The semantic typography, palette,
> theme, and accessibility rules are normative and partly machine-enforced.
> Layout and composition sections are review guidance; existing optical values
> are not all tokenized. Update this guide, implementation tokens, and affected
> tests together when the contract changes.

## Scope and sources of truth

Orbit is the management surface in
`phases/05-query-service/src/ui-spa/`. It covers Overview, Intake, Explore,
Review, Verification, Admin, observability, authentication, help, and their
shared shell. It does not re-theme the loopback robot upload console, the
public Ingotron website, or marketing documents.

The sources of truth are deliberately separated:

1. This guide defines the product-level design rules and allowed exceptions.
2. `phases/05-query-service/src/ui-spa/src/styles/app.css` defines the runtime
   Orbit tokens, including the paired light/dark accent palettes and type scale.
3. `phases/ui-kit/` defines reusable component behaviour and cross-SPA
   semantics. Orbit may map those semantic tokens onto its own visual theme.
4. Page CSS consumes semantic type and color tokens and composes layouts on the
   shared spacing grid. Repeated spacing values should become shared tokens.

When the guide and implementation disagree, treat that as drift to fix rather
than silently choosing one. Behaviour, endpoints, permissions, and operational
claims remain governed by the Phase-05 technical and user documentation.

The words **must**, **should**, and **may** describe requirements,
recommendations, and optional choices respectively.

## Design principles

### Calm operational clarity

Orbit is a professional operations product. It should make fleet state,
exceptions, and the next safe action obvious without looking theatrical.
Hierarchy comes from type, spacing, borders, and alignment—not effects.

### Dense, never tiny

Operational pages can contain substantial data, but density must come from
layout, progressive disclosure, and column priority. It must never come from
shrinking visible text below the type scale or compressing controls until they
are hard to target.

### Evidence before decoration

Prefer a timestamp, reason, count, or source over a decorative visual. A status
must say what happened and, when useful, when it happened. Do not invent data
or imply backend capabilities that do not exist.

### One restrained accent

The ice-blue accent identifies focus, selection, and the primary action. It is
not ambient
decoration. Orbit uses flat surfaces, crisp one-pixel borders, and measured
contrast. Gradients, glow, glass blur, and decorative drop shadows are not part
of the system.

### Local and dependable

Fonts and UI assets must be shipped with the application. The UI must remain
usable on an air-gapped deployment and must not rely on a CDN or remote asset
at runtime.

## Typography

### Families

- Use `--rv-font-sans` (`Instrument Sans Variable`, followed by the declared
  sans-serif fallbacks) for interface copy.
- Use `--rv-font-mono` (`IBM Plex Mono`, followed by the declared monospace
  fallbacks) for IDs, hashes, timestamps, metrics, counts, dense numeric
  columns, code, queries, commands, and the short uppercase kicker labels
  above panels.
- Do not use monospace merely to make a label feel technical.
- Use tabular numerals for changing metrics and aligned numeric columns when
  the font context supports them.

Instrument Sans is installed through `@fontsource-variable/instrument-sans`
(weight axis) and IBM Plex Mono through `@fontsource/ibm-plex-mono` (400 and
500 only). Do not add a network font import. Their OFL-1.1 notices are served
at `/ui/licenses/instrument-sans-OFL-1.1.txt` and
`/ui/licenses/ibm-plex-mono-OFL-1.1.txt`.

### Type roles

The browser root remains 16px. The following roles are the complete Orbit type
scale:

| Role | Size token | Leading token | Weight | Typical use |
|---|---|---|---|---|
| Caption | `--rv-type-caption`: 12px | `--rv-leading-caption`: 16px | 400 or 500 | Eyebrows, field labels, metadata, table headers, status text |
| UI | `--rv-type-ui`: 13px | `--rv-leading-ui`: 18px | 400 or 500 | Navigation, buttons, inputs, compact table cells |
| Body | `--rv-type-body`: 14px | `--rv-leading-body`: 20px | 400 | Descriptions, help, notices, empty-state copy |
| Small title | `--rv-type-title-sm`: 16px | `--rv-leading-title-sm`: 22px | 500 or 600 | Panel and card titles |
| Medium title | `--rv-type-title-md`: 20px | `--rv-leading-title-md`: 28px | 500 or 600 | Major section titles and drawer titles |
| Metric | `--rv-type-metric`: 24px | `--rv-leading-metric`: 28px | 500 or 600 | KPI values and compact numeric callouts |
| Large metric | `--rv-type-metric-lg`: 36px | `--rv-leading-metric-lg`: 36px | 500 | Headline numbers in the Overview pipeline strip and the Review class scores; mono |
| Page | `--rv-type-page`: `clamp(32px, 3vw, 40px)` | `--rv-leading-page`: 1.1 | 600 | One page title per route |

Visible text must never be smaller than 12px. This includes badges, chart axes,
tooltips, tags, timestamps, table headers, placeholder text, and responsive
variants. If content does not fit, change the layout, truncate with access to
the full value, hide a low-priority column, or add disclosure.

### Weight and tracking

Use only the following weight tokens:

| Token | Value | Use |
|---|---:|---|
| `--rv-weight-regular` | 400 | Body and secondary UI copy |
| `--rv-weight-medium` | 500 | Controls, labels, emphasized data |
| `--rv-weight-semibold` | 600 | Titles and decisive emphasis |

Use only the following tracking tokens:

| Token | Value | Use |
|---|---:|---|
| `--rv-tracking-normal` | 0 | Body, UI copy, data |
| `--rv-tracking-label` | 0.06em | Short uppercase eyebrows and category labels |
| `--rv-tracking-heading` | -0.015em | Small and medium titles |
| `--rv-tracking-display` | -0.035em | Page titles and large metrics |

Do not use arbitrary variable-font weights or tracking values. Uppercase is
reserved for short labels such as `DEFECT`, `RISK`, `OBS`, and compact
eyebrows; sentences and actions use sentence case.

## Color

Orbit supports dark and light modes through one semantic token set. Dark is the
default: a graphite ground, warm off-white text, and one ice-blue accent. Light
remaps the same roles onto a warm paper ground with a deep blue accent. Color must be referenced through
semantic tokens rather than literals in page or component CSS.

### Theme behaviour

- The global theme control in the shell switches the management UI between
  **dark** and **light**. The resulting choice applies to every route,
  including the authentication callback; it is not an Explore setting and does
  not change server state or the URL.
- Dark is the first-visit default and the fallback when storage is missing,
  unavailable, or contains an unknown value. Orbit does not implicitly follow
  the operating-system preference in this two-mode contract.
- An explicit choice is stored as `dark` or `light` under
  `localStorage["robot-vault.theme"]`. Authentication initialization and sign
  out must not clear it. A storage failure may prevent cross-session
  persistence but must not prevent changing the current tab.
- Apply the stored theme synchronously before React mounts. The root `.dark`
  class, `data-theme` value, and native `color-scheme` must agree so the first
  application frame, form controls, and scrollbars do not flash the wrong
  theme.
- Theme changes apply immediately and should synchronize to other open tabs
  through the browser `storage` event.
- The control's accessible name describes the next action: **Switch to light
  theme** or **Switch to dark theme**. Its icon is decorative, and its keyboard
  focus treatment follows the same rules as every other shell utility.

A future `system` mode requires an intentional contract and migration. Absence
of a stored choice means dark today; it must not silently acquire new meaning.

### Dark neutral and brand palette

| Token | Value | Role |
|---|---|---|
| `--rv-bg` | `#0d0f12` | Application background |
| `--rv-surface` | `#14171b` | Primary panels |
| `--rv-surface-raised` | `#1a1e23` | Controls and nested surfaces |
| `--rv-surface-hover` | `#21262c` | Hovered interactive surface |
| `--rv-surface-subtle` | `#0a0c0e` | Navigation sidebar and recessed regions |
| `--rv-surface-selected` | `#18222e` | Selected row, card, or navigation item |
| `--rv-line` | `#252a31` | Standard border and divider |
| `--rv-line-strong` | `#343a43` | Emphasized boundary |
| `--rv-text` | `#eceae4` | Primary text |
| `--rv-muted` | `#a3a8b0` | Secondary text |
| `--rv-dim` | `#8c9199` | Tertiary text on approved dark surfaces |
| `--rv-accent` | `#9ccbff` | Focus, selection, primary accent |
| `--rv-accent-hover` | `#b8d9ff` | Accent hover foreground |
| `--rv-accent-soft` | `#18222e` | Accent-tinted surface |
| `--rv-accent-subtle` | `#1f3247` | Stronger accent-tinted surface |
| `--rv-accent-line` | `#4a6a8f` | Accent boundary |
| `--rv-accent-text` | `#c4e0ff` | Accent text when higher contrast is needed |
| `--rv-control-border` | `#4a525c` | Standard dark control border |
| `--rv-on-accent` | `#0b1520` | Text and icons on a solid accent control |

### Light neutral and brand palette

| Token | Value | Role |
|---|---|---|
| `--rv-bg` | `#f6f5f1` | Application background |
| `--rv-surface` | `#ffffff` | Primary panels |
| `--rv-surface-raised` | `#faf9f6` | Controls and nested surfaces |
| `--rv-surface-hover` | `#efede7` | Hovered interactive surface |
| `--rv-surface-subtle` | `#efeee9` | Navigation sidebar and recessed regions |
| `--rv-surface-selected` | `#eaf2fc` | Selected row, card, or navigation item |
| `--rv-line` | `#e2e0da` | Quiet structural divider |
| `--rv-line-strong` | `#cfccc4` | Emphasized structural divider |
| `--rv-text` | `#16181b` | Primary text |
| `--rv-muted` | `#4e535b` | Secondary text |
| `--rv-dim` | `#666b73` | Tertiary text on approved light surfaces |
| `--rv-accent` | `#1f5fa8` | Focus, selection, primary accent |
| `--rv-accent-hover` | `#184c87` | Accent hover foreground |
| `--rv-accent-soft` | `#eaf2fc` | Accent-tinted surface |
| `--rv-accent-subtle` | `#d4e5f9` | Stronger accent-tinted surface |
| `--rv-accent-line` | `#9dc0ea` | Quiet accent-tinted divider |
| `--rv-accent-text` | `#184c87` | Accent text when higher contrast is needed |
| `--rv-control-border` | `#8a8d93` | Boundary required to identify a control |
| `--rv-on-accent` | `#ffffff` | Text and icons on a solid accent control |

Use the accent for the current location, keyboard focus, a primary action, or a
small state indicator. Do not fill large page regions with it, apply it to
every link, or use it as a substitute for hierarchy.

### Dark semantic palette

| Token | Value | Role |
|---|---|---|
| `--rv-green` | `#74c69d` | Healthy, completed, verified |
| `--rv-green-soft` | `#12201a` | Healthy-state background |
| `--rv-green-line` | `#2c4a3a` | Healthy-state boundary |
| `--rv-amber` | `#f0b04a` | Degraded, delayed, attention |
| `--rv-amber-soft` | `#251c0f` | Attention-state background |
| `--rv-amber-line` | `#5c4520` | Attention-state boundary |
| `--rv-red` | `#ff7b6b` | Failed, destructive, blocked |
| `--rv-red-soft` | `#2a1513` | Failure-state background |
| `--rv-red-line` | `#6a302a` | Failure-state boundary |
| `--rv-info` | `#86a9d6` | Informational, neutral status accent |

### Light semantic palette

| Token | Value | Role |
|---|---|---|
| `--rv-green` | `#1e7a4c` | Healthy, completed, verified |
| `--rv-green-soft` | `#ecf7f0` | Healthy-state background |
| `--rv-green-line` | `#9ad3b2` | Quiet healthy-state divider |
| `--rv-amber` | `#9a5b00` | Degraded, delayed, attention |
| `--rv-amber-soft` | `#fdf5e6` | Attention-state background |
| `--rv-amber-line` | `#f0c977` | Quiet attention-state divider |
| `--rv-red` | `#b42318` | Failed, destructive, blocked |
| `--rv-red-soft` | `#fdeeec` | Failure-state background |
| `--rv-red-line` | `#f2a9a0` | Quiet failure-state divider |
| `--rv-info` | `#2f5f9a` | Informational, neutral status accent |

Color never carries status alone. Pair it with a label, icon, pattern, or
explicit value. Do not use green for ordinary selection or the accent for
success.

Normal text and its background must meet WCAG AA contrast of at least 4.5:1;
large text and meaningful graphical boundaries must meet at least 3:1. Re-test
contrast when changing a foreground, tint, opacity, or nested surface. Never
reduce text opacity as a shortcut for hierarchy.

The approved light pairs were measured as follows (WCAG 2 relative luminance,
2026-10-10):

| Pair | Contrast |
|---|---:|
| Primary text / application background | 16.31:1 |
| Muted text / application background | 7.10:1 |
| Dim text / raised surface | 5.09:1 |
| Accent / application background | 5.90:1 |
| Accent / accent-soft surface | 5.71:1 |
| Accent / accent-subtle surface | 5.02:1 |
| Accent text / accent-soft surface | 7.68:1 |
| On-accent text / solid accent | 6.44:1 |
| Green / green-soft surface | 4.85:1 |
| Amber / amber-soft surface | 5.01:1 |
| Red / red-soft surface | 5.83:1 |
| Control border / raised surface | 3.16:1 |
| Control border / white surface | 3.33:1 |

In dark mode every text token measures at least 5.28:1 on the surfaces it is
approved for (dim text on the raised surface is the lowest pair).

`--rv-line`, `--rv-line-strong`, `--rv-accent-line`, and the pale semantic line
tokens are intentionally quiet structural dividers in light mode. They do not
claim 3:1 and must not be the sole affordance that identifies an interactive
control or communicates state. Inputs use `--rv-control-border`; focus uses the
accent outline; status uses contrast-safe text plus an explicit label or icon.
These measurements are regression floors, not permission to combine a token
with an untested surface.

## Spacing and geometry

### Spacing scale

Use a 4px base grid. These are the standard spacing steps:

| Token | Value |
|---|---:|
| `--rv-space-0` | 0 |
| `--rv-space-1` | 4px |
| `--rv-space-2` | 8px |
| `--rv-space-3` | 12px |
| `--rv-space-4` | 16px |
| `--rv-space-5` | 20px |
| `--rv-space-6` | 24px |
| `--rv-space-8` | 32px |
| `--rv-space-10` | 40px |
| `--rv-space-12` | 48px |
| `--rv-space-16` | 64px |

Choose the nearest named step instead of introducing one-off gaps and padding.
Fixed shell geometry, chart plots, and optical icon alignment may use another
multiple of 4px when a named step cannot express the requirement. Document any
non-grid exception next to the rule that needs it.

### Controls and targets

- Compact controls are 32px high; standard controls are 36px; prominent form
  controls are 40px.
- On touch layouts, the clickable area should be at least 44 by 44px even when
  the visible control is smaller.
- A dense desktop table row should be at least 40px high. Use disclosure or
  horizontal scrolling before reducing its type or target size.
- Inputs in one filter group use the same height and baseline.

### Radius, borders, and depth

- Use `--radius-sm` (5px) for compact controls, `--radius-md` (7px) for standard
  controls, and `--radius-lg` (10px) for panels and dialogs.
- Use `999px` only for pills, status chips, and circular indicators.
- Decorative dividers use a one-pixel `--rv-line` or `--rv-line-strong`
  border. A light-mode boundary required to identify a control uses
  `--rv-control-border`; active controls use the accent focus outline. Semantic
  line tokens supplement a readable status label or icon rather than carrying
  the meaning alone.
- Nested panels should normally be separated by a divider or background change,
  not another stack of rounded cards.
- Do not use gradients, glow, backdrop blur, glass effects, or decorative box
  shadows. Dialog backdrops may dim content without blurring it.

### Icons

Use the installed `lucide-react` library. Standard interface icons are 16px;
compact utility icons may be 14px; prominent empty-state or page icons may be
18–20px. Keep stroke width visually consistent, normally 1.5–2. An icon-only
button must have an accessible name and a visible tooltip when its meaning is
not universal.

### Motion

Interaction transitions should take 120–160ms and change color, border, or
opacity only. Avoid layout animation, bounce, parallax, pulsing decoration, and
continuous ambient motion. Progress indicators may animate while work is
active. Under `prefers-reduced-motion: reduce`, remove non-essential animation
and smooth scrolling without hiding state changes.

## Shell, layout, and navigation

Orbit uses a stable shell so page structure feels predictable:

- On desktop, a 248px sidebar sits on the left. It is sticky, full height, and
  scrolls on its own. The page content uses the remaining width, is centred,
  and is at most 1400px wide.
- The sidebar shows, from top to bottom: the brand, **Overview**, the
  **Pipeline** group, the **System** group, and a footer with the posture
  chips, the account control, and the theme control.
- The Pipeline group follows the data flow as a numbered track: **01 Intake**,
  **02 Explore**, **03 Review**, and **04 Verification**. Review and
  Verification remain edition gated because their server routes do not exist
  in Core.
- Pipeline items may show a live count from `/api/v1/metrics/overview`:
  uploads in the last hour on Intake, episodes awaiting QA on Review (amber
  when above zero), and completed exports in the last 24 hours on Verification.
  A null, loading, or failed value shows no count; it never shows 0. The count
  is hidden from assistive technology and repeated as the link's description,
  so the link name stays the page name.
- **Robots**, **Admin**, **Observability**, and **Help** are system utilities,
  not workflow steps. Observability may remain hidden when its backing profile
  is unavailable. Help opens the help panel.
- Every page starts with the shared `PageHeader` component
  (`components/PageHeader.tsx`): a mono uppercase kicker, one `h1`, one line of
  supporting copy, and an optional cluster of actions on the right. Put a live
  state, such as "Refreshes every 30 seconds", in that cluster with
  `PageStatus`. Its dot shows the tone and its words carry the meaning. The
  header sets no outer margin; the page layout's gap spaces it.
- The kicker names the page's place in the sidebar. Pipeline pages use the
  step number and sidebar label, for example `03 · Review`. System pages use
  `System / Robots`, and the guide uses `Guide / Help`.
- Do not add a second navigation strip below the header. The sidebar already
  shows where the operator is.
- Prefer one dominant workspace plus a supporting panel over a grid of equally
  weighted cards.

The responsive bands mirror the production shell:

| Band | Width | Expected change |
|---|---:|---|
| Desktop | Above 1120px | Sidebar with kickers, track connector, counts, and footer |
| Tablet | 701–1120px | Sidebar becomes a top bar: brand and utilities, then one horizontally scrolling row of pipeline and system items |
| Mobile | 461–700px | Posture chips and tagline hide; columns stack |
| Narrow mobile | Up to 460px | Only the brand mark remains of the brand; content remains complete |

Max-width queries may implement these bands at `1120px`, `700px`, and `460px`.
In the tablet and mobile bands, navigation items and utilities are at least
44px high. Page grids and dense controls may add a content-driven breakpoint
inside a band when their real labels or data no longer fit. Keep those queries
scoped to the owning page, change composition rather than shrinking type or
targets, and verify the layout immediately above and below each threshold.
Because the content column is narrower than the window, page breakpoints must
be chosen against the content width, not the window width.

## Components and interaction patterns

### Navigation and rails

Show active state with text, border, and surface—not color alone. The active
sidebar item uses `aria-current="page"`, the selected surface, an accent
border, and an accent ring on its track node. Navigation labels remain stable
between pages. Horizontal navigation may scroll on small
screens but must preserve keyboard access and a visible focus ring.

### Headings and panels

Every route has one page title. Panels use a small or medium title and may have
one caption-level eyebrow or metadata line. A panel must group related content,
not merely decorate whitespace. Avoid panel-within-panel chains deeper than two
levels.

### Metrics

Use the metric role for the value, a caption or UI role for its label, and an
explicit unit. A missing metric renders `—` with an unavailable explanation;
it must not render `0`. Trends need a stated comparison period. Decorative
sparklines may supplement but never replace the number.

### Buttons and actions

Each region should have one visually primary action at most. Use verb-first,
specific labels such as **Download manifest**, **Create cut**, and **Retry
query**. Destructive actions require a danger treatment and confirmation when
the result cannot be recovered. Disabled controls must explain the unmet
condition nearby or through accessible help.

### Forms, search, and filters

Labels remain visible; placeholder text is an example or hint, not the only
label. Search uses the same query vocabulary as results. Applied filters must
be reviewable and removable, and a reset must not silently discard an unrelated
saved view. Validation appears next to the field and in an accessible summary
when multiple fields fail.

### Tables and lists

Table headers use the caption role; cells use UI by default and body only for
long explanations. Numeric columns align by decimal or right edge. IDs and
timestamps may use mono, but prose does not. Sorting exposes direction in text
and semantics. On smaller screens, keep identity, status, and the next action;
move secondary columns into row detail before considering horizontal scroll.

### Status pills

Pills are for finite states, not general metadata. Use the shared status-domain
mapping so upload, episode, export, and PII terms retain one tone everywhere.
The visible label must be understandable without its color. Unknown values use
a neutral treatment and remain visible for diagnosis.

### Drawers, dialogs, help, and toasts

Use a drawer for contextual inspection that benefits from retaining the list;
use a dialog for a bounded decision or mutation. Both must trap focus, provide
an accessible name, close with Escape when safe, and restore focus to the
opener. Toasts acknowledge transient outcomes but do not contain the only copy
of an error or a required next step. The onboarding tour opens only when the
user requests it from Help.

### Commands and machine values

Commands, PromQL, SQL, hashes, object keys, and dataset IDs use mono at no less
than the caption size. Long values may truncate visually only when copy access
or an expanded view exposes the complete value. Copy actions announce success
without replacing the value.

## Charts and data states

### Charts

Use the installed ECharts integration and the Orbit semantic tokens.

The series order and chart chrome are fixed theme mappings:

| Token | Light | Dark | Use |
|---|---|---|---|
| `--rv-chart-series-1` | `#0f766e` | `#58c2ae` | Primary series |
| `--rv-chart-series-2` | `#2563eb` | `#7f9de8` | Comparison series 2 |
| `--rv-chart-series-3` | `#7c3aed` | `#a68bdd` | Comparison series 3 |
| `--rv-chart-series-4` | `#b45309` | `#c79a55` | Comparison series 4 |
| `--rv-chart-series-5` | `#0e7490` | `#6faaa1` | Comparison series 5 |
| `--rv-chart-series-6` | `#475569` | `#8c99a5` | Comparison series 6 |
| `--rv-chart-series-7` | `#be123c` | `#c07777` | Comparison series 7 |
| `--rv-chart-series-8` | `#4d7c0f` | `#8cb77f` | Comparison series 8 |
| `--rv-chart-text` | `#52525b` | `#788682` | Axis and legend copy |
| `--rv-chart-axis` | `#a1a1aa` | `#33413e` | Axis rules and ticks |
| `--rv-chart-grid` | `#e4e4e7` | `#1d2826` | Structural grid |
| `--rv-chart-slider` | `#f4f4f5` | `#0d1211` | Data-zoom surface |
| `--rv-chart-selection` | `#ccfbf1` | `#173b35` | Selected data range |
| `--rv-chart-tooltip-bg` | `#18181b` | `#121817` | Tooltip surface |
| `--rv-chart-tooltip-border` | `#3f3f46` | `#2b3936` | Tooltip boundary |
| `--rv-chart-tooltip-text` | `#fafafa` | `#edf4f2` | Tooltip copy |

These tokens are series identifiers, not replacements for the semantic status
palette. A series keeps its assigned index when the theme changes so its
identity does not shift.

- Axis labels and legends use caption size; tooltips use UI size.
- The primary series is the accent and normally 2px wide. Additional series use
  distinguishable, contrast-tested colors; reserve green, amber, and red for
  their semantic meanings.
- Grid lines use `--rv-chart-grid`; axis and annotation copy use the chart text
  and axis tokens.
- Avoid gradients, glow, faux depth, unnecessary area fills, and animated
  entrances. Points appear on hover or when individual observations matter.
- Include units, time zone or range, aggregation/bucket size, and a textual
  value or table path when the chart carries operational meaning.
- Do not connect across missing data in a way that implies continuity.
- Chart colors must be derived from the active theme at option-construction
  time. Axis, label, grid, tooltip, zoom, annotation, and series colors must not
  be hard-coded to the dark palette in TypeScript.
- A theme change must rebuild or update every mounted ECharts option. Canvas
  pixels do not inherit changed CSS variables automatically.
- In light mode, tooltip surfaces use a contrast-tested dark tooltip with light
  text or the approved light surface/text pairing. Zoom controls and data-area
  fills remain distinguishable without reducing label contrast.
- Review chart-series contrast against the plot background and against adjacent
  series in both modes. A textual legend or table path remains required because
  hue alone is not an accessible series identifier.

### Required states

Every data-bearing region must distinguish these conditions:

| State | Treatment |
|---|---|
| Loading | Skeleton or compact progress copy that preserves approximate layout |
| Empty | Explain that the source contains no records and suggest a valid next step |
| Filtered empty | State that no records match and provide a clear filter reset |
| Unavailable | Use `—` or an unavailable label; identify the failed source when useful |
| Error | Plain-language summary, retained technical detail, and a retry when safe |
| Stale | Keep last valid data, label it stale, and show the last-updated time |
| Partial | Render valid regions and mark failed regions independently |
| Success | Confirm the resulting object or state, not merely “Done” |

Zero is a valid measurement. It must never be used as a fallback for missing,
failed, or not-yet-loaded data.

## Responsive behaviour

- Test the shell and every route in both themes at 1440px, 1120px, 700px, and
  393px widths.
- A page must remain operable at 320 CSS pixels and at 200% browser zoom.
- Preserve content order from most important to least important when columns
  stack. Do not duplicate interactive controls between desktop and mobile DOMs.
- Filters stack before their labels truncate. Actions wrap or move into a
  labelled overflow menu before their text disappears.
- Drawers may become full-width sheets on mobile. Dialogs retain visible title,
  content, and action regions without trapping controls below the viewport.
- Horizontal scrolling is acceptable for primary navigation, command blocks,
  and irreducibly tabular data. The page itself must not scroll horizontally.
- Charts must resize without illegible axes; reduce tick count before reducing
  caption size.

## Accessibility

Orbit targets WCAG 2.2 AA.

- Use semantic landmarks, one logical `h1`, ordered headings, real buttons and
  links, and native form elements wherever possible.
- All workflows must be complete with a keyboard. Focus is always visible in
  both themes with the accent two-pixel outline and must not be obscured by
  sticky chrome.
- Dialogs and drawers trap and restore focus. New errors and asynchronous
  completion messages use appropriate live-region semantics without repeatedly
  announcing polling updates.
- Controls have accessible names; icon-only controls do not rely on `title`
  alone. Images and charts have appropriate alternatives.
- Status, selection, validation, and chart series do not rely on color alone.
- Respect reduced motion and high zoom. Reflow must not hide information or
  require two-dimensional page scrolling.
- Keep visible labels associated with inputs and make error recovery explicit.
- Automated checks supplement, but do not replace, keyboard, screen-reader,
  contrast, zoom, and responsive review.

## Content and vocabulary

The product and protocol name in this repository is **Robot Vault**. **Orbit**
is the management UI shell name. **Ingotron** is reserved for the public website
and marketing material; do not introduce it into the product UI **as a name**. The one exception is the
edition name "Ingotron Pro" in the "Available in Pro" panel.

**Carve-out: machine values are not brand vocabulary.** This rule governs what we *call* the product, not what the machine
*is* called. The robot agent keeps its Ingotron names: the binary
`ingotron-agent`, its install root `/opt/ingotron-agent`, its environment
variables `INGOTRON_*`, and the enrollment code prefixes `ign1.` and `ignr1.`.
They are normative literals. Where the UI must show one, it shows the
literal, exactly, and the rule is:

- **Allowed:** inside a command block, a copyable command, an inline `<code>`
  span, a URL, a file path, or an identifier — anywhere the string is a value
  the operator will type, paste, or click rather than read as English. The
  robot install, re-enroll and agent-upgrade commands on the Robots page (a
  `curl` of the vault's own `/robot` address, then `sh robot.sh --vault …`) are
  the ones that ship.
- **Forbidden, unchanged:** page titles, headings, navigation, empty states,
  toasts, button labels, prose, and anything a reader parses as the product's
  name. "Ingotron is ready" is a violation; `ingotron-agent` in a command block
  is not.

The failure direction settles it. A UI that suppressed or paraphrased these
literals to satisfy the vocabulary rule would print a command that does not
exist, while an operator enrolls a robot — and no rendering of the sentence
tells them the real command. The brand risk it would avoid is that a screenshot shows a
lowercase binary name. Render the literal.

Use the workflow labels consistently:

| Navigation | Meaning | Established domain nouns |
|---|---|---|
| Overview | Fleet posture and current operational signals | fleet, robot, upload, stored bytes |
| Intake | Uploaded captures and capture context | upload, capture, topic, operator, site |
| Explore | Timeseries and read-only SQL investigation | topic, range, bucket, query |
| Review | Episode QA and PII assessment | episode, DEFECT, RISK, evidence, task label |
| Verification | Dataset cuts, exports, provenance, and offline checks | dataset cut, manifest, signature, export, datacard |
| Admin | Ingest administration | ingest, service, status |
| OBS | Optional Prometheus-backed observability | metric, alert, PromQL |

Additional content rules:

- Use sentence case for titles, labels, actions, and messages. Preserve
  conventional uppercase acronyms and defined statuses.
- Prefer concise, concrete sentences. State what the system knows, the source
  of that knowledge when relevant, and the safest next action.
- Use “dataset cut” for the immutable selected set and “export” for a generated
  downstream format. Do not use them interchangeably.
- Keep DEFECT and RISK separate. Never present an invented composite score.
- Use “unavailable” for a failed or absent source, “empty” for a valid source
  with no records, and “unknown” for an unrecognized value.
- Dates and times must expose their time zone or make the current range's time
  zone explicit. Pair numbers with units and use locale-safe grouping.
- Do not claim cancellation, deduplication, retries, PII safety, or verification
  coverage beyond the endpoint and evidence actually available.

## Implementation contract

### Token use

- Page and component CSS must use the `--rv-type-*`, `--rv-leading-*`,
  `--rv-weight-*`, and `--rv-tracking-*` tokens. Raw `font-size`, line-height,
  font-weight, or letter-spacing values belong only in the central token
  definitions or an explicitly documented browser-normalization exception.
- No visible type role may resolve below 12px at any viewport.
- Page CSS must use `--rv-*` or shared semantic color tokens. Literal colors
  belong only in central token definitions, third-party chart configuration
  derived from those tokens, or a documented compatibility fallback.
- Light roles are defined on `:root`; the dark roles are defined on
  `.dark`. Both selectors expose the same semantic token names. Typography,
  spacing, sizing, and radius tokens do not change with theme.
- Theme-dependent solid-control foregrounds use `--rv-on-accent`; do not assume
  that each theme pairs its solid accent with a contrast-safe on-accent color.
- Sticky headers, code surfaces, canvas charts, and other visually dark special
  cases still use theme tokens. A component must not pin `color-scheme: dark`
  or retain a literal dark surface in light mode.
- New repeated spacing, control sizes, and radii should become named tokens
  rather than extending the existing optical-value debt.
- Do not use `!important` for typography, color, or ordinary component
  overrides. Fix selector scope or component ownership instead. Reduced-motion
  rules may use it to suppress third-party or inline motion; a responsive rule
  may use it only when it must neutralize an inline geometry coordinate, with
  the reason documented beside the rule.

### Components and dependencies

- Reuse `@robot-vault/ui-kit` components and status mappings before adding a
  page-local equivalent.
- Use `lucide-react` for interface icons and ECharts for charts. Do not add a
  second icon, chart, component, or styling library for one page.
- Keep assets self-hosted and compatible with the release content-security
  policy and air-gap posture.
- Keep the global theme controller independent of authentication and route
  data. It owns `robot-vault.theme`, root theme attributes, cross-tab updates,
  and the accessible shell control; pages must not implement local toggles.
- Keep data and permission behaviour intact during visual changes. A redesign
  must not expose a route, action, or claim the backend does not support.

### Review gates

Before merging an Orbit UI change:

- [ ] Type roles, weights, tracking, and colors use documented tokens in both
      themes; new repeated spacing and radii use named tokens.
- [ ] No visible text is below 12px; tables, pills, chart labels, and mobile
      variants are included in the check.
- [ ] The result contains no gradient, glow, backdrop blur, or decorative
      shadow.
- [ ] Loading, empty, filtered-empty, error, partial, stale, and unavailable
      states affected by the change remain distinct.
- [ ] Keyboard traversal, focus visibility/restoration, accessible names, and
      live messages were checked.
- [ ] Contrast was checked in light and dark for every new
      foreground/background, boundary, and chart pair.
- [ ] The affected routes were reviewed in light and dark at 1440px, 1120px,
      700px, and 393px, plus 200% zoom.
- [ ] First load uses dark without a saved preference; light and dark choices
      survive reload and sign out; invalid/unavailable storage falls back
      safely; other open tabs synchronize.
- [ ] Theme changes update native controls, dialogs, code surfaces, skeletons,
      and mounted charts without a wrong-theme flash.
- [ ] Core/Pro gates, OBS availability, auth modes, and deep links still behave
      as documented when relevant.
- [ ] Focused tests, SPA typecheck, full Vitest suite, and production build pass.
- [ ] This guide and the user/developer documentation changed atomically when
      the design contract or visible vocabulary changed.

`npm run check:design --workspace robot-vault-ui` verifies the required central
theme/type/spacing token declarations, rejects raw page-level typography, and
prevents typographic `!important` overrides. It does not lint color usage —
the color-token rule above is enforced in review, not by the script — nor
every layout length, radius, or visual relationship. The full UI test command
runs this check before Vitest; screenshot review remains necessary.

## Token reference

### Typography

```css
--rv-font-sans: "Instrument Sans Variable", ui-sans-serif, system-ui, sans-serif;
--rv-font-mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Consolas, monospace;

--rv-type-caption: .75rem;
--rv-type-ui: .8125rem;
--rv-type-body: .875rem;
--rv-type-title-sm: 1rem;
--rv-type-title-md: 1.25rem;
--rv-type-metric: 1.5rem;
--rv-type-metric-lg: 2.25rem;
--rv-type-page: clamp(2rem, 3vw, 2.5rem);

--rv-leading-caption: 1.333;
--rv-leading-ui: 1.385;
--rv-leading-body: 1.429;
--rv-leading-title-sm: 1.375;
--rv-leading-title-md: 1.4;
--rv-leading-metric: 1.167;
--rv-leading-metric-lg: 1;
--rv-leading-page: 1.1;

--rv-weight-regular: 400;
--rv-weight-medium: 500;
--rv-weight-semibold: 600;

--rv-tracking-normal: 0;
--rv-tracking-label: 0.06em;
--rv-tracking-heading: -0.015em;
--rv-tracking-display: -0.035em;
```

The canonical color values are the palette tables above and their declarations
in `src/styles/app.css`: light under `:root`, dark under `.dark`. Tailwind/ui-kit
aliases map to the same roles (`background`, `foreground`, `card`, `border`,
`muted`, `accent`, and the semantic tones); they are not a third palette. The
central implementation may expose additional semantic aliases, but aliases
must resolve to those roles rather than creating new page-local colors.

## Page and pattern matrix

| Page | Primary pattern | Supporting pattern | Critical states |
|---|---|---|---|
| Overview | Fleet topology and operational summary | KPI row, attention rail, fleet table | Partial KPIs, quiet fleet, unavailable metric, robot attention |
| Intake | Robot-centred capture workspace | Search/filter band, upload table, detail drawer | In flight, completed, failed, filtered empty, missing context |
| Explore | Query and timeseries workspace | Saved views, SQL console, result table | No selection, no samples, query error, stale range |
| Review | Episode QA review workspace | Score/status filters, evidence drawer, create-cut dialog | No report, low DEFECT/RISK, raw PII, permission failure |
| Verification | Version rail and provenance dossier | Export pipeline, datacard, offline command | Unsigned, pending, exporting, failed export, dead letter, legacy provenance |
| Admin | Service posture workspace | Ingest counts and recent uploads | Loading, degraded service, unavailable status |
| OBS | PromQL and chart workspace | Alert list, CSV export | Profile absent, empty range, query error, active alert |
| Authentication | Centred bounded status card | Recovery link or progress state | Signing in, callback failure, session cleared |

Shared shell, page heading, panel, table, status, form, drawer/dialog, command,
chart, and state patterns should look and behave the same wherever this matrix
reuses them. Page identity comes from information architecture and content—not
a page-specific visual language.

## Governance

A new token or component variant requires a repeated need, a semantic name,
and review against existing roles. Do not add a token named after one page or a
single literal value. Record intentional exceptions in this guide and keep
them narrow.

Design-system changes use the same repository rule as implementation changes:
update documentation and tests atomically, finish green, and use a focused
conventional commit. Periodically audit page CSS for raw type/color values,
sub-12px text, one-off radii and spacing, `!important`, and prohibited effects.
