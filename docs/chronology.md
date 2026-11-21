# Chronology

**Case Tools → Chronology** opens the **Daily timeline** on the latest date with
activity in the case. Use **Previous day with activity** and **Next day with
activity** to skip empty dates, or choose any date directly. The day uses the
account or browser timezone, including changes for daylight saving time.
**Show UTC times** changes the displayed clock and keeps the selected local day.

## Reading a day

Entries run earliest first, with 25 per page after grouping. Each links to its
original or the saved grouping and supporting records. Archived sources are
labelled. An exact collection time can place evidence on the timeline even when
its event time is unknown.

| Entry | How to read it |
| --- | --- |
| Conversation instance | Opens the exact saved inferred session for the reported people. The first message on the selected day determines its position. Multi-day instances distinguish the day's messages from the full session. A labelled AI summary covers the full instance. Otherwise the preview quotes the first message on that day. Distinct overlapping instances retain a **Multiple groupings** label. |
| Message or call | A timed message outside a valid saved instance has its own row. Phone calls and FaceTime use the reported start and show duration when available. |
| Possible trip | Shows the vehicle, first and last reported places and times, and the number of supporting observations. Overnight trips appear on each day they overlap. Open the trip to inspect its observations. |
| Location observation | Shows the reported place and any person or vehicle explicitly linked to it. Individual vehicle observations remain visible when a current trip does not cover them. |
| Collected | Shows the source's recorded collection time. For example, a photo collected on Tuesday appears as **Collected** on Tuesday. A source with both an event time and a collection time can have two entries. |

Browsing uses existing [conversation instances](evidence.md#reading-conversations)
and [possible trips](analysis-locations.md#automatic-vehicle-trips). It starts no
analysis. Only timed originals and their source-backed groupings can place
activity on a day. Upload times and extracted date strings cannot establish an
event time.

## Recorded chronology and date references

Open **Recorded chronology** to browse the individual timestamps across dates.
Recorded events use communications' `occurred_at` and locations' `observed_at`.
**Acquisition times** uses `collected_at`. **Events and acquisitions** shows
both as separate rows. Open an original to inspect its reported time and source.

Filter by source type, explicit person/vehicle relationship, and inclusive dates.
Date filters use the account/browser timezone. Each row shows one timestamp.
**Show UTC times** switches the display while retaining the date filters and
page. Originals retain source wording and vehicle-import timezone interpretation. Person filters
use communication participants/person locations; vehicle filters use vehicle
locations. Cross-case/inconsistent related records are unavailable.

Events paginate at 25, ordered by timestamp with deterministic ties.
**Extracted date references** opens a separate list of retained `entities` Date
candidates under Browse all findings' latest-equivalent-run, source-validation,
and review rules. That list can be filtered by source type and person. The
chronology's date range and vehicle filters apply only to recorded timestamps.
Extracted strings lack a confirmed date/timezone and remain unplaced.

Every timeline, chronology, and source request requires current case access.
