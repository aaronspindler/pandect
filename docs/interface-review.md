# Current interface review

Selections, its brief exports, and its question form were subsequently removed.
Selection-related journeys below record earlier validation, not current capabilities.

Status: local review complete. The scope is every current interface family,
including public, account, investigator, and operator screens. This catalogue
records 24 findings/refinements, representative states, and validation limits;
it does not claim exhaustive state permutations or full accessibility compliance.
Apply [PRINCIPLES](../PRINCIPLES.md) and [DESIGN](../DESIGN.md) throughout.

## Acceptance

- Everyday destinations and actions have visible, understandable names.
- Required inputs, next actions, current filters, and consequential context are
  visible at the point of use. Supporting details have specific disclosure labels.
- Features remain reachable for the permitted roles, including without JavaScript.
- Empty, busy, error, conflict, and recovery states explain the next useful action.
- Desktop, tablet, mobile, keyboard, reflow, text enlargement, dark mode, and
  forced colors receive the checks required by DESIGN. Report evidence limits.
- Preserve case isolation, originals, provenance, saved notes and review reasons,
  consent, resource bounds, and backend authorization.

## Coverage catalogue

Review screen families and their meaningful states, rather than counting shared
template fragments as separate screens. There are currently 138 app-local HTML
templates and 215 route patterns, including downloads and mutation endpoints;
route counts do not establish screen coverage. Allauth and Django also supply
account/operator interfaces. The ignored `template_inventory.json` and
`route_inventory.json` record the current source inventory beside the captures.

| Family | Included work | Current review status |
| --- | --- | --- |
| Shared navigation | All cases, Case Tools, Evidence, Processing, Account; responsive layout | Labels refined and validated on the shared screens |
| Cases and collaboration | Case list/create, case home/settings, invitations, role changes, removal and deletion confirmations | Reviewed, including delivery failure, edit conflict, expiry and current role recovery; settings and unavailable-invitation actions refined |
| Evidence intake | Mixed-file upload, single record types, shared source details, duplicate review, validation and interrupted batches | Reviewed and refined; all ten record forms, duplicate/retry paths, and missing prerequisites checked |
| Evidence reader | Original, findings, Info, editing, archive/restore, deletion inventory and recovery | Ten original types, Info/history, archive/restore, deletion inventories and missing-original recovery reviewed; navigation, playback wording and keyboard scrolling refined |
| Findings and analysis | Catalogue, single findings/results, reviews, source selection, context, consent and retry | Reviewed and refined across chooser, catalogue, results/review, source/context selection, consent, retry and operation-specific presentation |
| Processing | Active work, progress, imports, history, filtering, reprocessing and recovery | Reviewed across history, filtering, progress, reprocessing, approval, import review and recovery |
| Reports and connections | Calls, trips, coverage, speed, duplicates and identifier connections | Reviewed; key report totals made visible, useful finding previews retained and Save to selection restored |
| Selections and synthesis | List/create/edit, add/remove sources/findings, briefs/exports, questions and approval | Reviewed, including rename conflict, remove/delete confirmations, exact-source analysis, unavailable references/exports and cited synthesis; phone excerpts refined |
| People and social accounts | Search, detail, creation/editing, linked material and protected deletion | Reviewed, including social-account management, linked material, role checks and archived-evidence protection; deletion entry refined |
| Vehicles | Search, detail, creation/editing, linked material and protected deletion | Reviewed, including edit conflict, viewers, protected deletion and populated trips/observations; location/import actions promoted with same-case preselection |
| Maps and locations | Map controls, coverage, trips, places and linked people/evidence | Reviewed, including complete paths/sources, POIs, no-JavaScript/keyboard access, tile/geometry failure and reviewed estimates; providers mocked |
| Imports | Vehicle-location and message CSV upload, interpretation, review, progress and errors | Reviewed, including timezone/participant/vehicle interpretation, duplicate skip/keep, progress, validation and source-row provenance |
| Conversations and calls | Conversation browsing/search, messages, context, call additions and previews | Reviewed in Chromium and WebKit, including the message details panel, inferred-session boundaries, source filters, scroll recovery, revocation and exact evidence round trips |
| Chronology | Search, filtering, provenance and source navigation | Recorded/acquisition times, local/UTC display, unplaced dates, filters and source navigation reviewed |
| Accounts | Sign-in, signup, verification, email/password recovery, timezone and sign-out | Reviewed, including closed registration and real links generated in test mail; invalid reset-link wording refined while valid allauth forms are inherited |
| Public pages | Home, product, evidence/analysis examples, trust, roadmap and hosting | Reviewed without JavaScript across required layouts/appearance modes; current and planned capabilities remain distinct |
| Errors and operator screens | 403/404/500, unavailable originals/results, scoped Django admin and deletion inventories | Reviewed; actual 500 handler staged at a test-only route, role/material errors and scoped confirmations checked; missing-original wording corrected |

## Findings and implemented refinements

Current-run synthetic screenshots are under ignored
`data/validation/interface-review-2026-11-17/`; `report.html` pairs the captures
with the findings. No investigation data or live service configuration is used.

| ID | Evidence | Finding and refinement |
| --- | --- | --- |
| UI-01 | 01-case-tools-before; 03-evidence-after-desktop; 04-evidence-after-mobile | Navigation required recognizing icons or discovering tooltips. Show labels at every width; use a wider rail and a separate navigation row on phones. |
| UI-02 | 01-case-tools-before; 06-case-tools-after | Case settings was inside Case details at the bottom of the case home. Give owners a direct Case settings header link. |
| UI-03 | 02-evidence-before; 03-evidence-after-desktop; 04-evidence-after-mobile | Editing, saving to a selection, and further analysis were grouped inside More. Give each a visible control, with available analysis under Analyze evidence. Keep secondary actions in More. |
| UI-04 | 02-evidence-before; 04-evidence-after-mobile | Filters was an icon, and Info/More labels disappeared on phones. Retain the labels on all three controls. |
| UI-05 | 07-case-tools-large-text-before; 08-case-tools-large-text-after | At 200% text size, header actions squeezed the title into a column of single characters and fixed columns cramped the tool directory. Let headers wrap, size the directory to its content, and let enlarged phone navigation use two columns. Check title height as well as horizontal overflow. |
| UI-06 | intake-readers/error-before and after: bulk-source-validation | A failed upload said “Enter a valid URL” while the affected source field stayed collapsed. Open invalid source details automatically and name the field in the error. Preserve the queue and successful uploads. |
| UI-07 | intake-readers/final: queue-timezone-bulk and queue-timezone-single | Queued collection-time fields hardcoded UTC while the server interpreted the form's active timezone. Reuse the form's label; browser checks verify Toronto 09:15 is retained as 13:15 UTC in July for both intake paths. |
| UI-08 | intake-readers/before: pdf; final: pdf and pdf-invalid-page | PDFs had only sequential page links and no recovery link for an invalid page. Add Page number/Go to page and Open first page, preserving collection filters and no-JavaScript operation. |
| UI-09 | intake-readers/prerequisite-before and final: missing-person-location and missing-vehicle-location | Empty required person/vehicle choices did not explain how to proceed. Show the prerequisite with a case-scoped Add person or Add vehicle link; foreign-case records do not suppress this guidance. |
| UI-10 | intake-readers/before and after: location-original-320x844 | Processing wrapped into a word fragment on a 320px phone. Remove unnecessary gaps/padding in that navigation row while retaining the labels and hit areas. |
| UI-11 | intake-readers/before and after: phone-call-original-320 | Unsupported audio led with parser/encoding terminology. Explain that it cannot be played in the reader and direct the user to download the original to listen; supported formats and bounds are unchanged. |
| UI-12 | analysis-processing/before: analysis-chooser-before-opening; final: analysis-selection-desktop and reflow | All analysis choices were concealed by four closed category headings. Show the labeled operations and descriptions in sections, using two columns when space permits. |
| UI-13 | analysis-processing/final: catalogue-page-two and catalogue-filtered | Catalogue filters started closed and history used implementation language. Open the filters initially and use Result history, Latest results, and All saved results, retaining the explanation of the default scope. |
| UI-14 | analysis-processing/before and final: catalogue-filtered | Searching could lead each finding with normalization/method text. Lead with the useful finding preview and retain the bounded exact-match excerpt under Matching search details when it differs. |
| UI-15 | analysis-processing/final: topic-recovery and external-rerun-approval | Downloads and retry lived inside Processing details, while populated results concealed limitations. Put Download results beside the title, keep limitations open initially, and expose Run again with its existing transfer explanation and approval. |
| UI-16 | analysis-processing/final: processing; role regression in save-roles-verified.log | Choose analysis was inside Run more checks. Move it beside Reprocess evidence. Catalogue/connection rows also omitted the permission context, hiding Save to selection from owners/editors; pass the existing context and verify viewer exclusion. |
| UI-17 | analysis-processing/final: quiet-long-info | The independently scrolling Info panel lacked a keyboard focus stop. Name and focus the panel so Page Down can reach long retained attribution and history without changing the original. |
| UI-18 | case-workflows/before and final: vehicle-detail; final: vehicle-location-preselected | A new vehicle led with empty trips while Add a location and import actions sat below the first screen. Promote them beside Edit vehicle; the location form preselects the current vehicle from case-scoped choices. |
| UI-19 | case-workflows/final: person-delete-entry and vehicle-delete-entry | Owner deletion links were concealed inside Record details. Keep each visible below the disclosure; backend access, retained-evidence protection and the separate confirmation remain. |
| UI-20 | case-workflows/before and final: selection-reflow | Remove squeezed phone excerpts into a narrow column. Put it below the full-width excerpt at narrow sizes, preserving desktop alignment and control sizes. |
| UI-21 | remaining-families/variants and variants-final: invitation-expired | Expired links offered unrelated account-repair actions. Reserve email/account recovery for an invited-email mismatch; expired/revoked links lead with owner-resend guidance and All cases. |
| UI-22 | remaining-families/reports-final: analysis-report-default and analysis-report-totals | Key report counts/durations/coverage were inside About this report. Expose existing primary totals before the record list; retain supporting calculations and method details below. |
| UI-23 | remaining-families/variants-final and variants-verified: missing-original | The document fallback simultaneously said the original was unavailable and downloadable. Remove that promise; missing/changed files ask for a retained-storage check, with existing download controls and authorization unchanged. |
| UI-24 | remaining-families/accounts and accounts-final: password-link-invalid | The invalid reset page said Bad Token. Use Password reset link unavailable with a direct new-reset action; inherit allauth's valid password form rather than duplicating it. |

These implementation boundaries change shared templates, CSS/browser code,
reader context, first-use guidance, browser journeys, and owning documentation.
They change no routes, persisted state, backend permissions, processing consent,
or deployment configuration. Supporting metadata, technical execution details,
and exact search-match details retain specific disclosures. Analysis choices,
ordinary result actions, and consequential limitations are visible initially.

## First-pass validation record

- `uv run --locked make check` against disposable PostgreSQL passed: 1,407 tests,
  repository hygiene, Ruff lint/format, lock/configuration checks, Django database
  checks, and migration drift detection. Logs: `make-check.log`.
- The full `make browser-check` ran 76 journeys in Chromium/WebKit. Seventy-five
  passed; the analysis-overview journey failed in both engines on an outdated
  88px height bound for icon-only navigation. Enlarged navigation now uses two
  columns and the labeled-control bound was updated. The corrected journey passed
  in both engines (`overview-final.log`). Full log: `browser-full-final.log`.
- Final focused checks cover shared navigation, evidence panels/density,
  no-JavaScript disclosures, the analysis overview, source-specific analysis, and
  shared selections/synthesis. All seven journeys passed in Chromium and WebKit
  on the final implementation (`final-journeys.log`).
- Fresh Chrome captures cover case home, the populated text reader, its analysis
  menu, and the direct settings route. Narrow navigation was inspected at 320px;
  the mobile reader capture is 390px. Automated journeys cover the additional
  DESIGN viewports and appearance modes, including readable enlarged headers.
  Eight inspected report captures, local assets, and anchors were checked.
- The required read-only Agentics simplicity review found no actionable
  overengineering. Its optional cleanup of redundant analysis-menu CSS was
  applied without changing the inherited positioning or viewport bounds.

This does not establish full accessibility compliance, other original-type
usability, model quality, hosted CI, or deployment. All runtime validation used
synthetic records and disposable PostgreSQL databases.

## Completion boundary

Every current family above has been reviewed through source inspection and fresh
synthetic browser evidence. The searchable report contains 142 inspected captures,
including before/after comparisons and staged recovery states. Historical pass
records below show how coverage accumulated; the final record closes their
remaining screen-family work. No deployment, hosted CI, live provider qualification
or manual screen-reader testing is implied.

## Intake and original-reader review

Artifacts for this pass are in `intake-readers/` beside the first pass. The
expanded report includes intake, missing prerequisites, invalid upload fields,
timezone labels, PDF page selection/recovery, and representative original types.

- Inspected all ten single-record forms at 320px without JavaScript: text,
  document, image, audio, location, person location, vehicle location, text
  message, phone call, and FaceTime. Required inputs remain visible; source and
  declared metadata use the existing labeled optional disclosure.
- Inspected text/long text, image, PDF/UTF-8, WAV audio, three location types,
  messages, and both call types. Source attribution, exact times, linked case
  records, original downloads, and retained call additions stay available.
- Checked bulk shared defaults/overrides, pause/resume, per-file validation and
  retry, exact duplicates/explicit override, interrupted confirmation, server
  size limits, invalid files, and revoked access between uploads.
- Fresh baseline: ten browser journeys passed. The hidden-error-field regression
  failed in both engines before the fix (`error-before.log`). Missing-record
  guidance failed for both location types before the fix (`prerequisite-before.log`).
- After refinement, sixteen browser journeys passed (`browser.log`). On the final
  first-use guidance and expanded ten-form coverage, seven focused journeys
  passed (`verified-browser.log`). The final compact PDF controls and 200% text
  check passed separately in both engines (`document-final.log`).
- Final `make check` passed all 1,407 application tests, repository hygiene,
  lint/format, configuration, database, and migration checks (`final-check.log`).
- The required read-only simplicity review found no actionable overengineering.
  The expanded report has 41 inspected captures; Chrome verified search by screen
  ID and navigation to a different screen while a filter was active.
- Chrome captured the intake overview. Its extension refused a synthetic file
  selection because file URL access is disabled; no extension settings changed.
  The upload interactions above were verified by the repository's approved
  isolated Chromium/WebKit harness, using synthetic files and databases.

At this stage, supporting Info/history, unknown/archived metadata variants and
operator deletion still awaited the later review passes below.

## Findings, analysis and processing review

Artifacts for this pass are in `analysis-processing/`. The report now includes
67 inspected captures; AP-01 through AP-26 cover the chooser, catalogue, result
and review states, consent/retry, context, reprocessing, and keyboard-scrolled Info.

- Eleven fresh baseline browser journeys passed (`baseline.log`). Captures
  demonstrated closed analysis categories, technical search excerpts, and
  supporting actions inside Processing details before refinement.
- `make check` passed all 1,408 application tests, repository hygiene, Ruff
  lint/format, configuration, database checks, and migration detection
  (`final-check.log`). The maximum-page regression stayed within its resource
  limits: 30 SQL queries, without loading canonical result JSON.
- Twenty-six analysis/processing browser journeys passed in Chromium and WebKit
  (`final-browser.log`). Coverage includes desktop/tablet/mobile/320px layouts,
  200% text, keyboard/no-JavaScript operation, dark mode, reduced motion, forced
  colors, paginated findings/reviews, partial results, source withholding,
  external approval/stale approval, OCR and retained geographic context.
- On the final small label/copy refinements, 18 catalogue tests passed
  (`catalogue-final.log`), the owner/viewer workflow passed in both engines
  (`owner-final-browser.log`), and both reprocessing journeys passed in both
  engines (`reprocess-final-browser.log`). The owner can follow the restored
  Save to selection action; the role regression also checks editors/viewers.
- Three live-update journeys passed in both engines (`live-final-browser.log`):
  completed findings, processing filters/focus/disclosures, hidden-tab behavior,
  retry after a failed refresh, and access revocation. Final repository-wide
  Ruff lint and formatting checks also passed.
- Long Info content receives keyboard focus and scrolls with Page Down at all
  five required widths without JavaScript. Retained attribution/history stays
  available and the original remains unchanged.
- The required independent, read-only simplicity review found no actionable
  overengineering or correctness issues. All 67 report images, local assets and
  anchors, and 100 local links in the eight changed guides were checked.

All data and inference responses were synthetic or mocked, with disposable
PostgreSQL databases. These checks do not qualify live model/provider behavior,
hosted CI, deployment, manual screen-reader behavior, or every remaining screen
family; those families were carried into the subsequent passes below.

## Case, entity and selection workflows

Artifacts are in `case-workflows/`. CW-01 through CW-26 extend the report to
93 inspected captures, including current connections and chronology from the
preceding workflow pass.

- Five fresh baseline browser journeys passed (`baseline.log`). They cover
  case creation/settings/invitation acceptance and removal, vehicle CRUD and
  edit-conflict recovery, person/social accounts, location entry, shared
  selections, cited synthesis, and portable briefs.
- The refined vehicle header exposes location entry/import before empty trips.
  Location entry preselects the same-case vehicle; a regression checks current,
  foreign and malformed choices without exposing foreign labels. Entity deletion
  links are outside supporting Record details; confirmations and evidence
  protection remain server-side.
- Phone selection excerpts use the full row, with Remove below them. Desktop
  alignment and control sizes are retained.
- `make check` passed 1,409 application tests, repository hygiene, Ruff,
  configuration, database and migration checks (`final-check.log`). Four focused
  browser journeys passed in both engines (`final-browser.log`); the expanded
  collaboration/vehicle capture journeys then both passed (`captures-final.log`).
  The final selection/brief/synthesis journey passed in both engines after the
  phone refinement (`selection-final.log`). Final repository-wide Ruff lint and
  formatting checks passed.
- Fresh screenshots include case creation, invite/accept/change/remove access,
  named deletion inventory, social account entry, source-linked findings,
  chronology's local/UTC basis and unplaced dates, and the retained brief manifest.
  Invitation mail stays in the test backend; external inference is mocked.
- The required independent, read-only simplicity review found no actionable
  overengineering or material correctness issues in the cumulative scoped diff.

At this stage, invitation delivery/expiry/error variants, selection/entity
conflicts, protected deletion and the other remaining families were carried
into the final pass below.

## Final families, recovery and validation

`remaining-families/` adds RR-01 through RR-49 to the report. Twenty-six fresh
browser journeys passed (`baseline.log`), covering maps/POIs, vehicle/message CSV
imports, conversations, public pages, timezone/account settings, scoped admin,
deletion inventories and live progress. Forty-eight map captures were copied
from the map harness's fixed ignored directory after checking their modification
times against this run; `map-capture-provenance.json` records their origins.

Additional disposable browser audits exercised account forms/403/404/500 and
case/selection/evidence recovery at all five DESIGN viewports, 200% text, dark
mode/reduced motion and forced colors without JavaScript. The 500 handler was
invoked at a test-only URL; missing originals, changed selection references,
expired invitations and concurrent edits were staged in synthetic fixtures.
Password-reset and email-confirmation links came from the in-memory test mail
backend. No external mail/provider request was made.

Final verification:

- `make check` passed all 1,409 tests, staged/outgoing-history hygiene, Ruff
  lint/format, lock/Compose configuration, Django database checks and migration
  drift detection (`verified-final-check.log`). The catalogue resource regression
  used 30 SQL queries without loading canonical result JSON.
- The complete documented `make browser-check` passed all 78 journeys in
  Chromium and WebKit (`full-final-browser.log`). The final report-total change
  then passed three focused report/pagination/viewer journeys in both engines
  (`reports-final.log`).
- All 26 case-interface tests passed (`case-recovery.log`). Six document-preview
  tests passed after the missing-original wording change (`document-recovery.log`).
- The final account/error audit and actual test-mail link audit both passed in
  both engines (`accounts-final.log`); the final staged recovery audit also passed
  in both (`variant-verified.log`). The valid password form still inherits allauth,
  and originals, roles, confirmations and exact selection references remain intact.
- The final independent, read-only simplicity review found no actionable
  overengineering or material correctness issues in the cumulative diff and new
  files. All 142 report images/assets/anchors and 110 local links in 11 changed
  guides were checked. Report search, clearing an active filter when navigating,
  keyboard index scrolling and 320px reflow passed in both browsers.

The scope is the current local interfaces and representative meaningful states,
not every combination of content, browser settings and service failure. Automated
and keyboard checks do not establish full WCAG compliance. Manual screen-reader
testing, real provider/model quality, live tile/geocoder availability, hosted CI
and deployment were not performed. Synthetic runtime resources are removed after
validation; captures remain ignored and are never committed or pushed.

## Findings cleanup · 17 November 2026

Findings now show their useful content once, with supporting measurements and
native references under Details. Message pairs retain reported participants,
localized times and source links; geographic subjects and writing-comparison
examples remain visible. Timing-only message-link candidates are hidden by default
with explicit reveal controls, accepted-finding exceptions and stable direct links.

Local validation used synthetic fixtures in Chromium and WebKit. The findings
journey covers keyboard/no-JavaScript operation, 1440×900, 1440×1024, 768×1024,
390×844 and 320px reflow, 200% text sizing, CSS 400% zoom emulation, light/dark
and forced colors. CSS zoom is not a manual browser-zoom or screen-reader audit.
Captures are under ignored `data/validation/analysis-browser/` with
`streamlined-finding-*` and `phase3-*-finding-*` names. The broader analysis,
conversation, image, document and map journeys passed after correcting hidden
geographic subjects and writing examples; the affected journeys were rerun.

`make check` passed with 1,452 tests. Later focused regression runs cover the final
pagination, reveal-navigation and presentation fixes. Logs are retained locally
under `data/validation/streamlined-findings/`. This is local validation, not a live
rollout; the projection migration follows the upgrade procedure in
[operations](operations.md#upgrades-and-hosted-deployment).
