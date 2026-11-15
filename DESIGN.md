# Pandect interface design standard

Make careful investigative work approachable for people without investigative or
software engineering experience. Users should understand where they are, what
they are looking at, and their next action without learning the implementation.

This file defines durable design requirements, not implementation status. Screen
specifications belong in [interface design](docs/interface-design.md), delivery
gates in [the interface plan](docs/interface-plan.md), and validation procedures
in [validation](docs/validation.md).

## Principles

- Give each screen a clear task, title, and primary action. Keep controls beside
  the material they affect, choose sensible defaults, and ask only for necessary
  decisions. Simplify confusing interactions before adding explanations.
- Open cases to Evidence, keep Findings beside it, and group supporting
  destinations under **Case tools**. Disclose optional metadata, saved notes,
  and history through a compact Info control on evidence readers.
- Use compact navigation, aligned rows, and progressive disclosure. Prioritize
  useful material, readable text, and generous hit targets over decoration.
- Follow web conventions: real links, native forms, predictable Back and
  Save/Cancel behavior. Core tasks must work with a keyboard and without JavaScript.
- Separate originals, source details, saved descriptions, and derived suggestions.
  Preserve entered values on errors and explain recovery and consequential actions.
- Take inspiration from Apple's clarity, hierarchy, consistency, and accessibility
  without reproducing operating-system chrome.

## Visual foundations

Share semantic CSS custom properties across screens; avoid page-specific colors,
remote fonts, arbitrary spacing, and dependencies added only for styling.

| Role | Light | Dark |
| --- | --- | --- |
| Page background | `#F5F5F7` | `#111113` |
| Content surface | `#FFFFFF` | `#1C1C1E` |
| Primary text | `#1D1D1F` | `#F5F5F7` |
| Secondary text | `#515154` | `#B8B8BE` |
| Decorative divider | `#D2D2D7` | `#48484A` |
| Control boundary | `#86868B` | `#8E8E93` |
| Action and focus | `#0066CC` | `#66A9FF` |
| Destructive text | `#B42318` | `#FF8A80` |
| Success text | `#16743A` | `#80D99B` |

Use white text on the light action color and `#111113` text on the dark action
color. Verify actual foreground/background combinations for hover, selected,
error, and disabled states. Decorative dividers cannot identify input boundaries.

- Follow `prefers-color-scheme` without requiring an account preference.
- Fonts: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
  system monospace only for technical values such as checksums.
- Body: `1rem`, line height `1.5`. Type scale: `0.875rem`, `1rem`, `1.25rem`,
  `1.5rem`, `2rem`. Do not shrink important text to increase density.
- Spacing: `0.25`, `0.5`, `0.75`, `1`, `1.5`, `2`, `3`, `4rem`. Radii: `0.5rem`
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

## Layout and density

Investigator screens use case navigation, a compact record list, and a substantial
reading pane. Marketing shares the brand, fonts, colors, and accessibility rules
in an editorial layout; its introductory headings may scale from `2rem` to `4rem`.
Label workspace illustrations as fictional and show only implemented capabilities.

- At `80rem` and above, evidence uses a `5rem` navigation rail, `21rem` record
  list, and flexible reader across the available viewport. All investigator screens
  share this compact rail, including case tools, All cases, and account screens.
  From `64rem` to `80rem`, retain the rail with a full-width list or detail page. Below `64rem`, use compact top navigation;
  below `48rem`, stack fields and supporting metadata.
- Desktop evidence rows have a `3.5rem` minimum height, a readable title link
  covering the row, and one metadata line. Keep secondary actions in the detail
  page. Target 12 complete rows at `1440 × 900` and 14 at `1440 × 1024` with ordinary
  synthetic titles at default zoom. Never clip text or controls to meet counts.
- Keep headings, search, filters, and pagination compact. Avoid repeated case
  titles and timestamps. Reading passages should measure about `65–70ch`; forms
  up to `42rem`; dense lists may use the available width.
- Name each pane accessibly. Independent scrolling needs visible scrollbars and
  keyboard access. At narrow widths/high zoom, use document scrolling and
  canonical detail pages.
- Give every item a real URL. Selection updates it, Back restores the previous
  selection, and returning to a list retains filters/page. Full-page navigation
  is the baseline; partial updates are optional.

## Accessibility

Target [WCAG 2.2 Level AA](https://www.w3.org/TR/WCAG22/); automated and keyboard
checks do not establish full compliance. Manual screen-reader testing is optional,
requires an explicit user request, and must not block handoff.

- Use landmarks, a skip link, one page heading, logical section headings, native
  controls, and an explicit current-navigation state.
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
| Navigation | Stable case context, real links, current location, predictable Back. |
| Create/edit | Named fields, explicit Save/Cancel, values retained after errors. |
| Read-only | Explain the role; omit unavailable mutations. Enforce access server-side. |
| Search/filter | Labeled controls, submit fallback, result count, clear action, filters in URL. |
| Empty/no results | Explain the collection or retain the query; offer a permitted next action. |
| Busy/success | Name the action, prevent repeat submission, confirm changes with a useful next link. |
| Error/access removed | Explain failure and recovery without secrets/storage paths; stop revoked work and return to All cases. |
| Edit conflict | Preserve proposed values and require review of newer saved details. |
| Archive/delete | Explain retention or removal, identify the target/consequences, provide safe cancellation; mutate through POST. |

Use everyday action names: **Create case**, **Add evidence**, **Save changes**,
**Invite collaborator**, **Archive**, **Restore**, **Download original**. Routine
flows must not require knowledge of parsers, models, queues, or deployment settings.
Use exact dates and an explicit timezone where time matters; relative dates may
supplement an accessible exact value.

Keep guidance local and brief. Put optional technical detail and history in named
disclosures; keep required fields, next actions, and decision-critical context
visible. Explain deletion, external processing, and consequential uncertainty
where they matter. Preserve confirmations and source attribution without repeated
warning banners or general caveats. Labels such as **AI suggestion** and
**Unreviewed** should distinguish results without an accompanying essay.

## Evidence reader

- Keep the original and Findings together with one title/download header. Disclose
  supporting metadata and history through a clearly named header control; keep
  actions grouped separately. Long originals need keyboard-accessible navigation.
- Give original text stronger desktop emphasis (`1.5rem` at wide sizes, `1rem` on
  narrow screens). Keep supporting controls concise; use Info for record details
  and More for secondary actions.
- Originals are read-only; changes create new evidence. Label saved investigator
  descriptions read-only and distinguish declared metadata from extracted values.
  Checksums establish integrity, not truth, file format, or identity.
- Keep a concise source line near the original, with full metadata/checksums in
  the supporting details. Shortened checksums need full inspection/copy access.
- Follow [evidence retention and deletion](docs/evidence.md): owners/editors can
  archive/restore; individual deletion requires a superuser with current case
  access and confirmation of the complete removal inventory. Changed inventories
  require renewed review. Keep ordinary-investigator whole-case deletion owner-only.
- Provide a file picker alongside optional drag/drop, visible batch states, and
  individual recovery. Show processing only when the server actually processes.
- Preserve captured text without trimming/normalization; browser newlines may
  differ from the source. Offer document upload when exact file bytes matter.
- Serve originals through authorized case routes. Never expose storage URLs or
  embed untrusted HTML, SVG, or documents as application content.

## Completion and design review

Walk through the task as a first-time user, including empty, error, and recovery
states and each relevant case role. Check current permissions, cross-case
isolation, retained originals, and full-page fallbacks.

Inspect `1440 × 900`, `1440 × 1024`, `768 × 1024`, and `390 × 844`, plus `320px`
reflow and zoom. Verify keyboard/focus behavior, labels/status announcements,
light/dark contrast, reduced motion, and forced colors. Capture desktop/mobile
screenshots and compare density and alignment with the selected reference.
Report material limits separately from implementation status.

References: [Apple accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility),
[interface design tips](https://developer.apple.com/design/tips/), and
[WCAG target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).
