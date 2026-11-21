# Pandect interface design standard

Make investigative work clear, precise, and approachable. Pandect should feel like
a capable tool for examining evidence: structured, deliberate, and grounded in
sources. Users should understand where they are, what they are viewing, and their
next action without needing specialist software knowledge.

This file applies [the project principles](PRINCIPLES.md) through durable
interface design requirements, not implementation status. Screen
specifications belong in [interface design](docs/interface-design.md); check
selection and executable procedures belong in [validation](docs/validation.md).

## Principles

- Give each screen a task, title, and primary action. Keep controls beside their
  material, choose sensible defaults, and ask only for necessary decisions.
  Simplify confusing interactions before adding explanations.
- Open cases to **Case Tools** with direct links to **Findings**, **Conversations**,
  **Trips**, and **Images**. Keep **Evidence** and **Processing** directly available; Processing owns
  active work, history, and rerun controls.
- Favor compact navigation, aligned rows, and progressive disclosure. Use strong
  hierarchy and consistent controls. Let evidence, source references, and useful
  actions establish the product's investigative character; decoration must not
  compete with reading or imply capabilities the product does not have.

## Visual foundations

Use the same semantic CSS custom properties, graphite and teal palette, and
system fonts across public pages, authentication, and investigator screens.
Shared foundations live in `ui/static/ui/primitives.css`; surface-specific
styles adapt layout and density without redefining the brand palette or fonts.
Avoid one-off page palettes, remote fonts, arbitrary spacing, and dependencies
added only for styling.

| Role | Light | Dark |
| --- | --- | --- |
| Page background | `#F4F7F7` | `#0B1519` |
| Content surface | `#FFFFFF` | `#111F24` |
| Primary text | `#14282E` | `#EDF4F5` |
| Secondary text | `#4B6169` | `#B0C1C5` |
| Decorative divider | `#D2DFE0` | `#30464C` |
| Control boundary | `#71878B` | `#789196` |
| Action and focus | `#12645E` | `#8BD5C4` |
| Action text | `#FFFFFF` | `#0B1519` |
| Selected surface | `#E3EFED` | `#173A36` |
| Destructive text | `#B42318` | `#FF8A80` |
| Success text | `#16743A` | `#80D99B` |

Use white text on the light action color and `#0B1519` text on the dark action
color. Verify actual foreground/background combinations for hover, selected,
error, and disabled states. Decorative dividers cannot identify input boundaries.

- Follow `prefers-color-scheme` without requiring an account preference.
- Fonts: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
  Use system monospace for technical values such as checksums and the short
  indexes and compact eyebrow labels across public and investigator screens.
- Body: `1rem`, line height `1.5`. Type scale: `0.875rem`, `1rem`, `1.25rem`,
  `1.5rem`, `2rem`. Use weight `650` for page headings and `600` for section
  headings and buttons. Do not shrink important text to increase density.
- Spacing: `0.25`, `0.5`, `0.75`, `1`, `1.5`, `2`, `3`, `4rem`. Radii: `0.375rem`
  for controls, `0.75rem` for occasional grouped surfaces.
- Prefer grouping and fine dividers to nested cards. Reserve shadows for overlays;
  avoid glass effects, decorative gradients, and giant rounded panels.
- Use one locally served, licensed outline icon set. Label actions with text and
  hide decorative icons from assistive tools. Icon-only controls, including labels
  hidden at narrow widths, need accessible names and hover/focus tooltips. Tooltips
  must stay within the viewport, remain hoverable, dismiss with Escape, and never
  replace the accessible name. Retain native title tooltips without JavaScript.

Use the [approved gothic arch identity](docs/design/brand/pandect-arch/README.md)
from `ui/static/ui/brand/`: oxblood on light surfaces, ivory on dark. Preserve
proportions and clear space; lettering is artwork and needs no downloaded font.
Linked logos need the accessible name **Pandect** and a `44px` hit area. Use readable
system-font text in forced colors and the standalone arch on ivory for app icons.

## Marketing identity

Public pages introduce an investigation tool through evidence, analysis, and
source attribution. Use a crisp technical layout: confident typography, aligned columns,
numbered sections, fine rules, and structured evidence illustrations. Keep the
arch identity prominent; avoid surveillance imagery, fake terminal output,
decorative network graphs, and generic claims about discovering truth.

- Use the shared graphite and teal foundations above. The approved logo remains
  oxblood in light mode and ivory in dark mode; do not recolor its artwork.
- Introductory headings may scale fluidly from `2.5rem` to `5rem`, with a compact
  line height and generous surrounding space. Keep supporting text readable and
  short. Use monospace sparingly for section indexes, evidence identifiers, and
  compact metadata; navigation, prose, and controls stay in system sans serif.
- Build hierarchy with fine rules, grids, and open space. Use modest `0.25–0.5rem`
  corners and restrained background patterns. Do not use decorative motion,
  glass effects, or nested panels without a clear grouping purpose.
- Static evidence showcases may use an ink surface (`#10262C`) in either theme,
  with primary text `#EDF4F5`, secondary text `#B0C1C5`, and teal `#8BD5C4`.
  Keep fictional labels, source attribution, and relevant uncertainty visible.
  Illustrations explain current capabilities; they are not live case material or
  interactive demonstrations. Do not draw controls that appear operable.
- Give Overview, Evidence & analysis, Trust, and Roadmap a common header, footer,
  heading treatment, and section rhythm. Adapt the content structure to the
  page's task: product story, capability directory, processing choices, or future
  work. Preserve direct links to formats, requirements, and source documentation.
- Catalogue methods retain visible outcomes and setup requirements, with native
  disclosures for detail. Filters and links work without JavaScript. Clearly
  separate available capabilities from planned and exploratory work; never imply
  that every installation has optional services configured.
- Reflow structured illustrations and multi-column content into document order on
  small screens. Keep essential labels and limitations readable at increased
  text sizes. Forced colors replace custom fills and patterns with system colors;
  selection and hierarchy must remain understandable without color.

The implementation and content boundaries belong in
[public marketing pages](docs/marketing.md), not in application screen rules.

## Layout and density

Investigator screens use case navigation, a compact record list, and a substantial
reader. These density requirements apply to the investigator workspace;
public pages follow the marketing identity above.

- At `64rem` and above, all investigator screens share a `7rem` labeled navigation
  rail. Below `64rem`, use compact top navigation. Below `48rem`, stack fields and
  supporting metadata. Keep navigation labels visible at every width, wrapping
  the navigation onto its own row when needed.
- From `72rem` wide and `36rem` tall, selected evidence uses a fluid record list
  between `20rem` and `26rem` alongside a flexible reader. The list fills the
  workspace when no record is selected. Below either threshold, the collection
  and detail each use the workspace width and scroll with the document.
- Desktop evidence rows have a `3.5rem` minimum height, a readable title link
  covering the row, and one metadata line. Keep secondary actions in the detail
  page. Target 12 complete rows at `1440 × 900` and 14 at `1440 × 1024` with ordinary
  synthetic titles at default zoom. Never clip text or controls to meet counts.
- Keep headings, search, filters, and pagination compact. Avoid repeated case
  titles and timestamps. Use fluid gutters and let multi-column views and
  structured results use the available width. Keep reading passages around
  `65–70ch` and forms up to `42rem`.
- Name each pane accessibly. Independent scrolling needs visible scrollbars and
  keyboard access. At narrow widths/high zoom, use document scrolling and
  canonical detail pages.
- Give every item a real URL. Selection updates it; Back restores the previous
  selection, and returning to a list retains filters/page. Full-page navigation
  is the baseline; partial updates are optional.

## Accessibility

Target [WCAG 2.2 Level AA](https://www.w3.org/TR/WCAG22/); automated and keyboard
checks do not establish full compliance. Manual screen-reader testing and system
accessibility changes require an explicit user request and must not block handoff.

- Core tasks must work with a keyboard and without JavaScript. Use native forms
  and controls, landmarks, a skip link, one page heading, logical section headings,
  and an explicit current-navigation state.
- Maintain `4.5:1` text contrast (`3:1` for qualifying large text and essential
  control/state indicators). Convey status and selection beyond color.
- Provide `44 × 44` CSS-pixel hit areas for standalone controls and record links;
  inline prose links follow normal text layout. This Pandect target exceeds the
  WCAG AA minimum.
- Use a contrasting `3px` focus outline with `2px` offset, retained in forced
  colors. Sticky content must not obscure focus; focus order follows reading order.
- Support `320` CSS-pixel reflow, `200%` text resizing, and `400%` browser zoom.
  Wrap long names, addresses, URLs, and hashes or provide accessible reveal
  controls. Never truncate the only accessible record name.
- No task may require dragging, hovering, color recognition, or precise gestures.
  Support password managers, paste, and standard authentication autocomplete.
- Associate labels, help, and errors with fields. Failed submissions retain values
  and focus a linked error summary. Announce save/upload outcomes through status
  regions without announcing every progress tick or moving focus on updates.
- Honor forced colors and reduced motion. Keep transitions brief (normally
  `150ms` or less) and nonessential motion removable.
- Prefer full-page flows to dialogs. A justified dialog needs a name, initial
  focus, Escape, focus containment, and focus return.

## Interaction and writing

| Pattern | Required behavior |
| --- | --- |
| Navigation | Stable case context and predictable Back, following the URL rules above. |
| Create/edit | Named fields and explicit Save/Cancel. |
| Read-only | Explain the role and omit unavailable mutations; permissions follow [AGENTS.md](AGENTS.md#case-authorization-and-evidence-integrity). |
| Search/filter | Labeled controls, submit fallback, result count, clear action, filters in URL. |
| Empty/no results | Explain the collection or retain the query; offer a permitted next action. |
| Busy/success | Name the action, prevent repeat submission, confirm changes with a useful next link. |
| Error/access removed | Explain failure and recovery without secrets/storage paths; stop revoked work and return to All cases. |
| Edit conflict | Preserve proposed values and require review of newer saved details. |
| Archive/delete | Explain retention or removal, identify the target/consequences, provide safe cancellation; mutate through POST. |

Use everyday actions: **Create case**, **Add evidence**, **Save changes**,
**Invite collaborator**, **Archive**, **Restore**, **Download original**. Routine
flows must not require knowledge of parsers, models, queues, or deployment settings.
Use exact dates and an explicit timezone where time matters; relative dates may
supplement an accessible exact value.

Keep guidance local and brief. Disclose optional detail/history; keep required
fields, next actions, and decision-critical context visible. Explain deletion,
external processing, and consequential uncertainty at the decision. Preserve
confirmations and source attribution without repeated warnings. Use concise
labels such as **AI suggestion** and **Partial coverage**.

## Evidence reader

- Keep the original and its complete, paginated findings list together. Place a
  **Findings (count)** jump near the title. Use
  **Info** for supporting metadata, saved notes, and history; **Edit details** and
  **Analyze evidence** for everyday actions. Keep secondary actions under **More**.
  Long originals need keyboard-accessible navigation.
- Give original text stronger desktop emphasis (`1.5rem` at wide sizes, `1rem` on
  narrow screens).
- Originals are read-only; changes create new evidence. Distinguish declared
  metadata, saved read-only investigator descriptions, and derived suggestions.
  Checksums establish integrity, not truth, file format, or identity.
- Keep a concise source line near the original, with full metadata/checksums in
  the supporting details. Shortened checksums need full inspection/copy access.
- Apply the [retention and deletion contract](docs/evidence.md#permanent-deletion)
  when presenting authorized actions and confirmation inventories.
- Provide a file picker alongside optional drag/drop, visible batch states, and
  individual recovery. Show processing only when the server actually processes.
- Follow [original capture rules](docs/evidence.md#investigator-workflow); offer
  document upload when exact file bytes matter. Never expose storage URLs or embed
  untrusted HTML, SVG, or documents as application content.

## Completion and design review

Walk through the task as a first-time user across relevant case roles, empty/error
states, recovery, and full-page fallbacks. Apply the
[contributor validation requirements](AGENTS.md#validation-and-handoff).

Inspect `1440 × 900`, `1440 × 1024`, `768 × 1024`, and `390 × 844`, plus `320px`
reflow and zoom. Verify keyboard/focus behavior, labels/status announcements,
light/dark contrast, reduced motion, and forced colors. Capture desktop/mobile
screenshots and compare density and alignment with the selected reference.
Report material limits separately from implementation status.

References: [Apple accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility),
[interface design tips](https://developer.apple.com/design/tips/), and
[WCAG target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).
