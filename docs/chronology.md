# Chronology

**Case Tools → Chronology** opens the **Daily timeline** on the latest date with
activity in the case. Scroll down for older dates or up for newer dates.
Scrolling skips days without entries. Choose a **Date** to open that exact day,
including days without entries. You can also press Enter or select **Go to date**.
If scroll loading is unavailable, use **Older dates** or **Newer dates**.

Days follow the account or browser timezone, including changes for daylight
saving time. **Show UTC times** changes the displayed clock and keeps the
selected local day.

## Reading a day

Entries run earliest first, with 25 per page after grouping. Each links to its
original or the saved grouping and supporting records. Archived sources are
labelled. A **Collected** entry dates acquisition. Its original also appears under
**Unplaced evidence and time claims** when its event time is unknown.

| Entry | How to read it |
| --- | --- |
| Conversation instance | Opens the exact saved inferred session for the reported people. The first message on the selected day determines its position. Multi-day instances distinguish the day's messages from the full session. Summary previews label full or partial coverage. Without a saved summary the preview quotes the first message on that day. Distinct overlapping instances retain a **Multiple groupings** label. |
| Message or call | A timed message outside a valid saved instance has its own row. Phone calls and FaceTime show the reported start and a calculated end when the original supplies a duration. |
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
Date filters use the account/browser timezone. **Show UTC times** switches the
clock and keeps the filters and page. Results paginate at 25. The database uses
stable tie-breakers for paging. Equal readings and overlapping intervals can
leave the sequence of events unknown.

Open **Source clock details** for the imported row, parser version, interpreted
export timezone and original wording when available. Message delivery, read or
edit clocks and vehicle receipt clocks describe different actions. A supplied
offset can disagree with an export timezone, and receipt can precede a reported
observation. Both readings remain inspectable. Manual records may retain only
the normalized timestamp. A calculated call end uses the reported duration,
including zero. An absent duration leaves the end unknown.

On a cited person or vehicle observation, **Saved journey interpretations**
shows up to three current candidates and links to the full filtered findings.
Each interval runs between cited samples. Its largest sampling gap describes
the period between observations. Open the exact finding for its sources and
method. Other saved interpretations and the underlying observations remain available.

## Unplaced evidence and time claims

Open **Unplaced evidence and time claims** from either timeline view, even on
an empty day. **Originals without a known time of day** lists records by title,
including sources with only a collection or upload time. An imported message
with only a calendar date appears here with the reported day and export timezone.
It cannot place an event or a session at midnight. **Time claims** lists
retained date mentions and source-cited plans, commitments, reported events,
cancellations and corrections. Each list has its own pages. Source type and
reported person can narrow both. Event-date filters apply to dated rows only.

For example, a Tuesday message saying “Tomorrow’s pickup is cancelled” has a
reported message time on Tuesday. The word “Tomorrow” is a relative reference.
A saved cancellation suggestion has its own quotation, source and method
version. Neither places a pickup on Wednesday. Plans and later cancellations
can both appear. An investigator can inspect each one independently.

**Extracted date references** remains a date-only list. Version 2 of
**Extract identifiers and dates** also retains “today”, “yesterday” and
“tomorrow” with their original spelling and exact text offsets. Earlier results
keep their original method and coverage. Any later run follows the normal
processing workflow.

The lists use the latest eligible equivalent result, label partial coverage and
archived sources, and link to the exact finding and retained original. Missing
or invalid citations withhold the interpretation. Every timeline, finding and
source request requires current case access. Reading a timeline starts no
processing and sends no evidence to an external service.
