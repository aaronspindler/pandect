# Chronology

Open **Case tools → Chronology** to read explicit source timestamps across
communications and location observations. This is a read-only, case-scoped view;
there is no event authoring or inferred event database. Archived evidence remains
included and labeled.

Observed events use communications' `occurred_at` and locations' `observed_at`.
Choose **Acquisition times** for `collected_at`, or **Events and acquisitions** to
show both as separate rows. Creation time never substitutes for a missing source
or acquisition time. Each row links to its original evidence and identifies the
timestamp's meaning. A reported timestamp does not establish that an event
occurred.

Filters support source type, explicitly related person, vehicle, and inclusive
start/end dates. Dates and display use the active account/browser timezone;
the page also shows recorded UTC. Original source wording and vehicle-import
timezone interpretation remain available through the evidence reader. A person
filter uses only communication sender/recipient and person-location relationships.
A vehicle filter uses only vehicle-location relationships. Cross-case and
inconsistent related records remain unavailable.

The list uses 25 events per page, ordered by timestamp with deterministic ties.
**Unplaced date references** separately shows retained `entities` Date candidate
findings with the same latest-equivalent-run, source validation, and review
semantics as Browse all findings. These strings are not placed on the chronology
or interpreted into dates/timezones. The date-range filter cannot apply to them;
source/person filters still apply. Unplaced references have independent pagination.
A vehicle filter excludes them because these text findings have no explicit
vehicle association.

No remote services or inference are called when browsing chronology. Current
case access is required on every request and source link.
