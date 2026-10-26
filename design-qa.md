# Interface design QA

**Final result: passed.** No actionable P0/P1/P2 visual or core interaction
findings remain after the final comparison and regression runs.

## Comparison evidence

- Source visual truth: [refined option 3](docs/design/evidence-workspace-dense.png).
- Rendered implementation: [desktop](docs/design/implementation-desktop.png) and
  [mobile](docs/design/implementation-mobile.png), captured from the local Django
  application with a synthetic case, authenticated owner, selected Interview
  notes, and the Original section open.
- [Full-view comparison](docs/design/comparison-desktop.png): source on the left,
  implementation on the right, placed together before review.
- [Focused list comparison](docs/design/comparison-evidence-list.png): readable
  titles, metadata, controls, selected state, dividers, and pagination.
- Source and desktop implementation are both **1487 × 1058 pixels**, device scale
  factor 1. No density resampling was needed; the combined image is 2974 × 1058.
- Mobile capture: **390 × 844 CSS pixels**, device scale factor 1. Additional
  browser-suite captures cover 1440 × 900, 1440 × 1024, 768 × 1024, and 320-pixel
  reflow, plus dark, reduced-motion, enlarged-text, and forced-colors appearances.

The reference uses generated pane proportions. The implementation deliberately
uses the written standard's **208px sidebar and 320px collection** at ordinary
text size. It shows more records than the mock while leaving more reading width.
The synthetic record titles and original passage match the visual reference;
added timestamps reflect the real fixture-creation time in UTC. These are
intentional data/layout differences, not claims of pixel-identical reproduction.

## Findings and comparison history

1. **[P1, fixed] Native form submissions failed CSRF validation.** Browser testing
   found that `no-referrer` suppressed the Origin on ordinary form POSTs.
   Private pages now use `same-origin`, blocking cross-origin referrers while
   preserving CSRF. CSRF protection stayed enabled. Chromium and WebKit sign-in,
   edits, invitations, and no-JavaScript uploads now complete.
2. **[P2, fixed] Mobile case navigation wrapped into excessive rows.** Decorative
   navigation icons are omitted at phone widths; labels remain visible with
   44px targets. The post-fix mobile capture shows one navigation row and more
   usable reading space without horizontal overflow.
3. **[P2, fixed] Forced-colors icons disappeared on a dark Canvas.** The local
   outline assets now invert under dark forced colors. The
   [post-fix capture](docs/design/implementation-forced-colors.png) shows the
   navigation and pagination glyphs while retaining native system colors.
4. **[P2, fixed] Fixed-pixel row line heights could overlap enlarged text.** Row
   line heights are unitless and the root respects the user's default font size.
   The browser suite checks enlarged title/metadata separation and reflow.
5. **[P2, fixed] Validation errors lacked consistent queue-field associations.**
   Per-file errors now set `aria-invalid`, reference their error text, and announce
   an actionable message. Corrected retries clear these attributes.
6. **[P1, fixed] Unknown upload outcomes could be removed
   without reconciliation.** Unresolved entries now keep their file, frozen
   metadata, and submission UUID, and cannot be removed/replaced until a retry
   checks the result. Confirmed validation failures remain editable.
7. **[P2, fixed] Styled buttons overrode the browser's hidden state.** A global
   `[hidden]` rule now preserves intentional control visibility. The two-engine
   interruption test verifies that unresolved uploads cannot expose Remove, and
   retries verify the persisted record, checksum, submission UUID, and storage
   key without creating another original.

The initial comparison used browser-suite captures with compact test excerpts.
The final side-by-side comparison uses the richer synthetic reference passage
and record titles. Mobile navigation, toolbar height, and forced-colors fixes
were recaptured after implementation changes. Application and focused browser
regressions provide the interaction evidence; raster mockups are not used to
claim functional or accessibility validation.

## Required fidelity surfaces

| Surface | Review result |
| --- | --- |
| Typography | System font, 16px body/title rows, 14px secondary metadata, readable originals and wrapped long content; one page h1. |
| Spacing/layout | Three-pane desktop structure; 56px ordinary rows; measured 164px list chrome; canonical mobile detail pages. |
| Colors/tokens | Shared light/dark semantic tokens, visible focus, non-color selected/archived states, and verified palette contrast. |
| Asset quality | Locally vendored, unmodified MIT-licensed Tabler outline icons. No untrusted file is embedded as app content. |
| Copy/content | Originals, investigator descriptions, source details, and declared metadata stay distinct. Archive clearly retains material. |

## Interaction and accessibility evidence

- Full application permission/integrity suite against isolated PostgreSQL.
- Real sign-in, case/person creation and edits, captured text, archive/restore,
  sequential file batches, validation recovery, invitations, downgrade/revocation,
  and no-JavaScript upload/download in Chromium and WebKit.
- Keyboard link traversal, visible focus, native filters, accessibility labels,
  landmarks, error associations, status announcements, and browser script errors.
  macOS WebKit uses native Option-Tab link traversal; other engines use Tab.
- Measured at least **12 complete rows at 1440 × 900**, and **14 at 1440 × 1024**,
  with normal synthetic titles and default zoom.
- Phone/tablet layouts, 320 CSS-pixel reflow (the layout equivalent of 400% zoom
  at 1280px), and 200% root text-size emulation. These checks are not a claim of
  testing every browser's native zoom implementation.
- Native accessibility-tree inspection and keyboard testing were performed;
  there was no separate manual VoiceOver speech audit or formal WCAG certification.
- No browser script errors were observed in the passing principal journeys or
  final visual capture. External SMTP delivery and hosted CI/deployment remain
  outside local validation.

## Follow-up polish

- The implementation uses a visible Search submit control and Archive action for
  clear full-page/keyboard operation, rather than relying on the mock's closed
  overflow menu or JavaScript-only search.
- The file originals view intentionally offers authorized downloads without
  previews, OCR, transcription, or file-format verification, as specified for v1.

## Final qualification checklist

Final local qualification passed: **177 application tests**, four browser
journeys in **both Chromium and WebKit**, lock/lint/migration/database/Compose
configuration checks, isolated Compose startup/persistence/production static
delivery, and the locked dependency audit (no known vulnerabilities). Hosted CI
and real SMTP delivery were not run. The browser harness uses Django's standard
multipart encoder to restore fixture bytes omitted by WebKit's request
interception API; the interrupted-commit check still hits the real authenticated
view, commits a record, loses confirmation, and verifies the same original on retry.

- [x] Inspect source and implementation together, including the focused list.
- [x] Check all five fidelity surfaces and capture desktop/mobile evidence.
- [x] Qualify permissions, immutable originals, and standard application checks.
- [x] Qualify production static assets, Compose persistence, and dependency audit.
- [x] Finish the two-engine upload interruption/proxy-failure regression.
- [x] Confirm final result after the last regression run.
