# Quiet evidence workspace: design QA

final result: passed

The final combined comparison found no remaining P0/P1/P2 differences. This report
lives in `docs/`, following the repository's documentation ownership rule.

## Visual truth and captures

- Selected visual: [option 1](quiet-evidence-reference.png), generated at
  1487 × 1058 pixels and normalized to the 1440 × 1024 comparison viewport.
- Matching implementation: local synthetic evidence, light appearance,
  Chromium at 1440 × 1024 CSS pixels, device scale factor 1. The capture is
  `data/validation/quiet-evidence/chromium-quiet-evidence-1440x1024.png`.
- Full and focused comparisons are saved in
  `data/validation/quiet-evidence-preview/comparison-v2-{full,reader,collection}.png`.
  Source and implementation appear together in each comparison image.
- The in-app browser was also inspected directly with synthetic data. It follows
  the user's dark appearance. Its desktop, mobile, and 320px Info captures are
  `data/validation/quiet-evidence-preview/{desktop-v2,mobile-v2,info-320-v2}.png`.
  Temporary viewport overrides were reset after review.

These captures and the preview use fictional material. They are local validation
artifacts, not private investigation records or live deployment evidence.

## Comparison history

1. The first combined comparison found P2 drift in the major proportions and
   reading hierarchy: the rail/list were too narrow and original text was too
   small. The search field also showed a duplicate input border. Evidence:
   `comparison-v1.png` and `comparison-v1-{reader,collection}.png` in the same
   preview artifact directory.
2. The revised layout uses an 80px rail, 336px collection, 32px selected title,
   and 24px original text on wide screens, with the original's comfortable
   reading measure and stronger separation before Findings. The inner search
   border was removed. Current evidence: the three `comparison-v2-*` captures.
3. Narrow Info initially exceeded the viewport because of invalid CSS arithmetic.
   Corrected bounds were verified in the in-app browser at 320px: the inspector
   spans x=16 to x=304 and the document scroll width remains 320px. Its native
   summary and close control remain available.
4. The mobile toolbar now uses labelled, accessible 44px icon controls for Info
   and More beside Download original. Findings keep their icon beside the value.
   Long retained attachment filenames wrap in their original content section.

## Fidelity surfaces

- **Typography:** existing system fonts and semantic weights; strong selected
  title and original, smaller source metadata, and value-first findings. List
  titles remain 16px. Narrow original text remains 16px. Generated-image glyph
  metrics and exact line breaks are not treated as a new font requirement.
- **Spacing and layout:** the rail, collection, and reader follow the chosen
  hierarchy without a disclosure stack or nested cards. The list retains the
  documented 12/14 visible-row targets. Info is one flat inspector; More groups
  secondary actions. Mobile uses a compact bar and canonical reader.
- **Colors:** existing light/dark tokens, blue selection/focus, oxblood/ivory
  branding, and neutral dividers. The light comparison and separate dark captures
  evaluate the corresponding appearances without changing system preferences.
- **Assets:** approved transparent arch symbols, with preserved proportions and
  light/dark variants; existing licensed Tabler icons. The generated screen is a
  reference, not a raster replacement for working controls or evidence.
- **Copy and data:** routine instructional paragraphs and duplicate headings are
  removed. Original content, source links, uncertainty, and processing errors
  remain explicit. Real processing supplies one phone candidate for the sample;
  the mock's illustrative date finding is not fabricated. CSV uploads retain
  their actual Document category. Source character anchors replace fictional
  line numbers where that is what the processor supplies.

## Interaction evidence

The in-app browser verified search, selection with retained filters, Info opening
and closing, More, Edit details and safe cancellation back to Info, and 320px
inspector bounds. Its browser console contained no errors. The working synthetic
preview remains open; no deployment or investigation-data changes were needed.

Repository browser journeys additionally cover keyboard/Escape/outside dismissal,
no-JavaScript access, full originals and private downloads, current roles,
processing/history pagination, check-submission recovery, and responsive states.
Automated checks and screenshots do not establish full accessibility compliance
or model accuracy. Manual screen-reader testing was not requested.

## Follow-up polish

- Optional P3: the open Info inspector repeats its native Info trigger above the
  Evidence info heading. A later refinement could consolidate that header while
  preserving the no-JavaScript toggle. It does not obstruct content or controls.

The final visual review checked source and implementation separately, then the
combined full, reader, and collection inputs, plus mobile/Info and dark captures.
Implementation validation completed locally:

- The documented `make check` passed all 967 tests, Ruff lint/format checks,
  lockfile validation, system/database checks, migration drift, and Compose config.
- All 35 affected browser journeys ran in Chromium and WebKit. After repairing
  filename wrapping and stale test interactions/labels, all four affected journeys
  passed on rerun; the other 31 had already passed.
- The required shared simplicity review found no actionable overengineering or
  optional simplicity changes. It excluded concurrent unrelated checkout work.

These results do not claim CI execution or deployment of the running Docker app.
