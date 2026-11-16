# Quiet evidence workspace: design QA

Historical QA, archived **2026-11-15**. Results and artifact paths describe that
snapshot; [DESIGN.md](../../DESIGN.md) and [validation](../validation.md) own
current requirements. Final combined comparison found no remaining P0/P1/P2
differences.

## Visual truth and captures

- Selected [option 1](../design/quiet-evidence-reference.png): generated at
  1487 × 1058, normalized to 1440 × 1024.
- Matching implementation: synthetic evidence, light appearance, Chromium at
  1440 × 1024 CSS pixels, device scale 1;
  `data/validation/quiet-evidence/chromium-quiet-evidence-1440x1024.png`.
- Full/reader/collection comparisons show reference beside implementation:
  `data/validation/quiet-evidence-preview/comparison-v2-{full,reader,collection}.png`.
- Direct in-app browser inspection followed the user's dark appearance:
  `data/validation/quiet-evidence-preview/{desktop-v2,mobile-v2,info-320-v2}.png`.
  Temporary viewport overrides were reset.

Captures and preview use fictional material; they are local validation artifacts,
not investigation records or deployment evidence.

## Comparison history

1. Initial P2 differences: narrow rail/list, small original text, and duplicate
   search border. Evidence: `comparison-v1.png` and
   `comparison-v1-{reader,collection}.png` in the preview directory.
2. Revised layout: 80px rail, 336px collection, 32px selected title, 24px wide-screen
   original text, comfortable reading measure, stronger Findings separation,
   and no inner search border. Evidence: the three `comparison-v2-*` captures.
3. Invalid CSS arithmetic caused narrow Info overflow. The corrected inspector
   spans x=16–304 at 320px; document scroll width stays 320px. Native summary and
   close control remain available.
4. Mobile Info/More became labelled 44px icon controls beside Download original.
   Findings retain icons beside values; long attachment filenames wrap.

## Fidelity surfaces

- **Typography:** existing system fonts/weights; strong title/original, secondary
  metadata, value-first findings. List titles and narrow original text stay 16px.
  Generated glyph metrics/line breaks impose no font requirement.
- **Layout:** selected rail/collection/reader hierarchy, no disclosure stack or
  nested cards, documented 12/14-row targets, flat Info, grouped More, and mobile
  compact bar/canonical reader.
- **Colors:** existing light/dark tokens, blue selection/focus, oxblood/ivory brand,
  neutral dividers. Separate appearance captures required no system changes.
- **Assets:** approved transparent arch variants/proportions and licensed Tabler
  icons. Generated screens remain references, not raster controls or evidence.
- **Copy/data:** removed repeated instructions/headings while retaining originals,
  sources, uncertainty, and errors. Actual processing supplies one phone candidate;
  no illustrative date finding was fabricated. CSV stays Document; real character
  anchors replace fictional line numbers.

## Interaction evidence

In-app checks covered search, filter-preserving selection, Info open/close, More,
Edit details/cancellation back to Info, and 320px bounds; no console errors. The
synthetic preview remained open. No deployment or investigation data changed.

Repository browser journeys covered keyboard/Escape/outside dismissal,
no-JavaScript access, originals/private downloads, current roles, processing and
history pagination, check recovery, and responsiveness. Automated checks and
captures do not establish full accessibility or model accuracy. Screen-reader
testing was not requested.

## Follow-up polish

Optional P3: open Info repeats its native trigger above Evidence info. A later
refinement could consolidate it while retaining the no-JavaScript toggle;
content and controls remain unobstructed.

Final visual review checked reference and implementation separately, combined
full/reader/collection inputs, mobile/Info, and dark captures. Local validation:

- `make check`: 967 tests, Ruff, lockfile, system/database, migration drift, and
  Compose configuration passed.
- 35 affected Chromium/WebKit journeys ran. Four passed after filename-wrapping
  and stale-interaction/label fixes; the other 31 had passed already.
- Required shared simplicity review found no actionable overengineering or
  optional simplifications; concurrent unrelated work was excluded.

These results do not establish hosted CI or deployment of the running Docker app.
