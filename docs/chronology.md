# Chronology

**Case tools → Chronology** shows explicit communication/location timestamps in a
read-only, case-scoped view, including labelled archived evidence. There is no
event authoring or inferred event database.

Events use communications' `occurred_at` and locations' `observed_at`.
**Acquisition times** uses `collected_at`; **Events and acquisitions** shows both
as separate rows. Creation time never fills missing timestamps. Rows identify
timestamp meaning and link to originals; reported times do not prove events occurred.

Filter by source type, explicit person/vehicle relationship, and inclusive dates.
Dates/display use the account/browser timezone alongside recorded UTC. Originals
retain source wording and vehicle-import timezone interpretation. Person filters
use communication participants/person locations; vehicle filters use vehicle
locations. Cross-case/inconsistent related records are unavailable.

Events paginate at 25, ordered by timestamp with deterministic ties.
**Unplaced date references** separately paginates retained `entities` Date
candidates under Browse all findings' latest-equivalent-run, source-validation,
and review rules. These strings are not interpreted into dates/timezones or placed
on the chronology. Source/person filters apply; date filters cannot. Vehicle
filters exclude them because they have no explicit vehicle association.

Browsing calls no remote services/inference. Every request/source link requires
current case access.
