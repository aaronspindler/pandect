# Interface review coverage

Choose meaningful states for each affected screen family and follow the
[design review](../DESIGN.md#completion-and-design-review). Use the executable
[browser checks](validation.md#browser-checks) with synthetic cases.

## Coverage catalogue

| Screen family | Journeys and states to review |
| --- | --- |
| Shared navigation and search | All cases, Case Tools, Evidence, Processing, and Account. Check responsive navigation, filtered and empty results, return context, and access loss. |
| Cases and collaboration | Creation, rename conflicts, invitation delivery and recovery, role changes, and the case deletion inventory. |
| Evidence intake and reading | File and manual entry, archived records, source metadata, originals and previews, validation and retries, and evidence deletion. |
| Findings, analysis and Processing | Source and method selection, consent, queued work and recovery, saved findings, report totals, and citation round trips. |
| Important items | Mark evidence and findings, add and inspect dated notes, remove a mark, and return from the saved source to the appropriate list page. See [Important items](cases.md#important-items). |
| People, vehicles, maps and chronology | Entity forms and linked records, protected deletion, trip preparation, map and date filters, place editing, and source navigation. |
| Conversations, calls and imports | Exact message and call source links, saved sessions, attachments, timezone and participant or vehicle interpretation, import review, and stopped work. |
| Instagram monitor pilot | Target approval, synthetic observer and captures, paused schedules, and recovery. |
| Accounts, public and operator screens | Sign-in and sign-out, verification and recovery, public capability pages, permission errors, unavailable content, and scoped Django admin. |

Review the relevant no-JavaScript and keyboard paths, narrow layouts, access
roles, validation errors, and recovery. Provider-dependent journeys use the
documented synthetic or mocked configuration for routine checks.

## Dated reviews

The [November interface reviews](archive/interface-review-2026-11.md) record
prior captures, test results and limits, including the
[26 November workspace review](archive/interface-review-2026-11.md#workspace-review-26-november-2026).
