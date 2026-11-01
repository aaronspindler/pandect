# Pandect interface design standard

Pandect should make careful investigative work feel approachable. The interface
must be calm, clear, and dependable: an investigator should understand where
they are, what they are looking at, and their next useful action.

This is the project's design standard. It defines requirements for future
interfaces; it does not claim that those interfaces are implemented. Detailed
screen specifications and delivery plans belong in `docs/`.

## Principles

- **Make the task obvious.** Give each screen a clear title and one primary
  action. Keep related controls beside the material they affect.
- **Make room for the investigation.** Use compact navigation, aligned rows,
  and progressive disclosure. Show useful records instead of decorative space.
- **Keep familiar behavior.** Use ordinary links, buttons, forms, browser Back,
  and predictable save/cancel flows. Core tasks must work with a keyboard.
- **Be quietly polished.** Take inspiration from Apple's clarity, hierarchy,
  consistency, and care for accessibility. Use web conventions appropriate to
  Pandect rather than reproducing operating-system chrome.
- **Keep evidence understandable.** Separate original content, source details,
  investigator descriptions, and any future derived suggestions. A checksum
  describes integrity, not truth or verified identity.
- **Respect the user's work.** Preserve entered values after validation errors,
  make save results explicit, and explain recoverable failures.
- **Explain consequences.** Distinguish archiving retained material from deleting
  it. Destructive actions require a deliberate confirmation.

## Visual foundations

Use semantic CSS custom properties shared across screens. Avoid page-specific
colors, arbitrary spacing, remote fonts, and dependencies added only for styling.

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

Use white text on the light-mode action color and `#111113` text on the
dark-mode action color. Derive hover, selected, error, and disabled tokens from
these roles and verify each actual foreground/background combination. Decorative
dividers do not replace the stronger boundary required to identify an input.

- Default appearance follows `prefers-color-scheme`; support both appearances
  with the same hierarchy. Do not require an account preference to use dark mode.
- Font stack: `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
  Use the system monospace stack only for checksums and other technical values.
- Body text is `1rem` with line height `1.5`. Use a restrained type scale of
  `0.875rem`, `1rem`, `1.25rem`, `1.5rem`, and `2rem`. Secondary labels may be
  smaller than body text; shrinking important text is not a density strategy.
- Spacing scale: `0.25`, `0.5`, `0.75`, `1`, `1.5`, `2`, `3`, and `4rem`.
- Corner radii: `0.5rem` for controls; `0.75rem` for occasional grouped surfaces.
  Prefer grouping and fine dividers to containers around every item.
- Use shadows only for genuine overlays. Avoid glass effects, decorative
  gradients, giant rounded panels, and nested cards.
- Use one consistent, locally served, appropriately licensed outline icon set.
  Provide text labels for actions; hide decorative icons from assistive tools.

### Brand identity

Use the [approved gothic arch identity](docs/design/brand/pandect-arch/README.md)
in the shared header and case navigation. Serve the optimized logo assets from
`static/pandect/brand/`, using oxblood on light surfaces and ivory in dark mode.
Preserve the artwork's proportions and clear space; its lettering is part of
the image and does not require a downloaded font. The linked logo has the
accessible name **Pandect** and at least a `44px` hit area. In forced-colors mode,
show readable system-font text in place of the image. Browser and touch icons
use the standalone arch on an ivory background.

## Layout and information density

Public marketing pages use the same system fonts, brand, semantic colors, and
accessibility requirements in a centered, responsive editorial layout. Their
introductory headings may scale from `2rem` to `4rem` to establish hierarchy;
investigator screens retain the compact type scale defined above. Marketing workspace
illustrations must be labelled as fictional and must not imply interactive
controls or capabilities that are not implemented.

The selected direction is a three-pane investigation workspace: case navigation,
a compact record list, and a substantial reading/detail pane. Dense means more
useful material in view while retaining readable text and generous hit targets.

- At widths of `80rem` and above, use a `13rem` case sidebar, a `20rem` evidence
  list, and a flexible reading pane. Use the available viewport instead of
  centering the whole application inside a card.
- Between `64rem` and `80rem`, keep the case sidebar and show either a full-width
  list or detail page. Below `64rem`, replace the sidebar with compact top
  navigation; below `48rem`, stack form fields and supporting metadata.
- A full desktop evidence row has a `3.5rem` minimum height, a readable title,
  and one compact metadata line. Use a title link whose hit area covers the row.
  Keep secondary actions on the detail page so they do not crowd every row.
- At `1440 × 900`, target at least 12 complete evidence rows without scrolling
  the list; at `1440 × 1024`, target at least 14. Test this with ordinary synthetic
  titles at default zoom. Longer text, larger text settings, and narrow screens
  may expand rows; never clip controls or enforce the count at their expense.
- Keep list heading, search, filters, and pagination compact. Put source details
  in a concise summary; disclose full integrity metadata on request. Avoid
  repeating large case titles, descriptions, and timestamps in every pane.
- Constrain long reading passages to approximately `65–70ch`. Use a comfortable
  form measure of up to `42rem`; dense lists may use the available width.
- Each pane has an accessible name. If panes scroll independently, preserve
  visible scrollbars and make scrolling operable by keyboard. At narrow widths
  and high zoom, use ordinary document scrolling and canonical detail pages.
- Every item has a real URL. Selection updates the URL, browser Back restores
  the previous selection, and returning to the list preserves filters and page.
  Full-page navigation is the baseline; partial updates are optional enhancement.

## Accessibility requirements

Target [WCAG 2.2 Level AA](https://www.w3.org/TR/WCAG22/). Verify keyboard behavior
and use automated accessibility checks during development. Manual screen-reader
testing is optional and is not a completion or handoff requirement. These checks
do not establish full accessibility compliance.

- Use semantic landmarks, a skip link, one page-level heading, logical section
  headings, native controls, and an explicit current-navigation state.
- Maintain at least `4.5:1` contrast for ordinary text and `3:1` for qualifying
  large text and essential control/state indicators. Communicate status and
  selection through text or shape as well as color.
- Provide `44 × 44` CSS-pixel minimum hit areas for standalone controls and
  record links. This is a Pandect usability target, stronger than WCAG AA's
  target-size minimum. Inline prose links follow normal text layout.
- Show a `3px` focus outline with `2px` offset using a contrasting focus token.
  Preserve it in forced-colors mode. Sticky content must not obscure focus.
- Support reflow at `320` CSS pixels, `200%` text resizing, and `400%` browser
  zoom. Long filenames, email addresses, URLs, and hashes must wrap or have an
  accessible reveal control. Never truncate the only accessible record name.
- Keep focus order consistent with reading order. No task may require dragging,
  hovering, color recognition, or a precise pointer gesture.
- Associate visible labels, help text, and errors with fields. Provide a linked
  error summary after failed submission and move focus there deliberately.
- Announce upload and save outcomes through an appropriate status region.
  Do not announce every progress tick or move focus on background updates.
- Allow password managers, paste, and standard autocomplete in authentication.
- Honor reduced motion and forced colors. State transitions should be brief
  (normally at most `150ms`) and nonessential motion must be removable.
- Use an accessible full-page flow before introducing a dialog. A justified
  dialog needs an accessible name, initial focus, Escape behavior, focus
  containment, and focus return.

## Interaction and writing patterns

| Pattern | Required behavior |
| --- | --- |
| Navigation | Stable case context, real links, current location, predictable Back. |
| Create/edit | Clearly named fields, explicit Save/Cancel, values retained on validation errors. |
| Read-only | Explain the user's role; omit unavailable mutations without treating UI visibility as authorization. |
| Search/filter | Labeled controls, explicit submit fallback, results count, clear-filter action, filters in the URL. |
| Empty collection | Explain what belongs here and offer the next action permitted by the user's role. |
| No results | Preserve the query and offer a clear way to broaden it. |
| Busy | Name the action in progress; prevent accidental repeated submission. |
| Success | Confirm what changed with a persistent message and a useful next link. |
| Error | Explain what failed, what was preserved, and how to recover; omit secrets and storage paths. |
| Access removed | Stop subsequent operations and explain that the user no longer has access; return to All cases. |
| Edit conflict | Preserve proposed values and ask the user to review newer saved details before retrying. |
| Archive/restore | State that originals remain retained and accessible; offer the reverse action. |
| Delete case/person/vehicle | Show the named target and consequences; cancel is safe; mutation requires POST. |

Use plain verbs: **Create case**, **Add evidence**, **Save changes**, **Invite
collaborator**, **Archive**, **Restore**, and **Download original**. Avoid exposing
model class names, storage keys, or provider details in routine product flows.
Use absolute dates with an explicit timezone where time matters; relative dates
may supplement an accessible exact value.

## Evidence-specific behavior

- Original files and captured text are read-only. Editing details must never
  replace an original. A changed original is a new evidence record.
- Label investigator-supplied descriptions and declared technical metadata.
  Do not imply automatic extraction, verified file format, or verified identity.
- Show provenance near the original; keep a complete checksum and technical
  details in an accessible disclosure. A shortened visual checksum needs a way
  to inspect/copy the complete value.
- Archive and restore are available to owners and editors. Archived evidence
  remains viewable and downloadable by current case members. Ordinary investigators
  retain evidence until owner-confirmed whole-case deletion. Superusers with case
  access can choose **Delete evidence** in Details and confirm individual deletion
  in the product, with explicit original/derived-file consequences and safe
  cancellation. The scoped Django admin also permits this for active staff
  superusers. Preserve import-source protections in both workflows.
- Uploads provide a normal file picker as well as optional drag and drop. Batch
  entries have visible states and individual recovery actions. Do not invent a
  processing stage when the server only stores an original.
- Pasted text is the text captured through the browser, whose newline handling
  may differ from the clipboard or a source file. Preserve received text without
  trimming or normalization. Offer document upload when exact file bytes matter.
- Serve originals through currently authorized case routes. Never expose private
  storage URLs or embed untrusted HTML, SVG, or documents as application content.

## Completion and design review

Before considering an interface complete:

1. Exercise its intended task, empty state, validation errors, and recovery path
   using synthetic material and each relevant case role.
2. Inspect the actual interface at `1440 × 900`, `1440 × 1024`, `768 × 1024`,
   and `390 × 844`; also check `320` CSS-pixel reflow and zoom.
3. Verify keyboard navigation, visible focus, labels, status announcements,
   light/dark contrast, reduced motion, and forced colors.
4. Capture relevant desktop/mobile screenshots and compare against the selected
   visual reference. Check information density as well as visual alignment.
5. Verify current server-side access, cross-case isolation, retained originals,
   and full-page fallbacks. Document material limitations honestly.

Review this file before visible changes. Keep detailed workflows and implementation
status in the documentation, and revise the standard when an intentional product
decision changes a durable design rule.

References: [Apple accessibility guidance](https://developer.apple.com/design/human-interface-guidelines/accessibility),
[Apple interface design tips](https://developer.apple.com/design/tips/), and
[WCAG target-size guidance](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html).
