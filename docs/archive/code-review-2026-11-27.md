# Pandect code review, 27 November 2026

Reviewed `main` at `3117b330098c3fca9fdf9f66f198adb3ed415491` with the
starting working tree, including 73 modified tracked files and three untracked
source or template files. The most detailed pass covered the pending changes and
their callers. The baseline pass followed high-risk paths through case access,
private evidence, imports, publication, search, Case Chat, live UI, and release
configuration. The findings below describe that first snapshot. The
[Astra recheck](#astra-62-ultra-recheck) records their later state.

P1 indicates a broad workflow failure, significant evidence handling issue, or
material shared-service impact. P2 indicates a workflow, reliability, or evidence
fidelity defect under a bounded condition. P3 indicates a narrower usability or
maintenance defect. All findings below have high source confidence unless a
qualification is stated. Source line links refer to the reviewed snapshot.

## P1

### AUDIT-001 — Findings can fail to render after a valid group-message analysis

**Origin** Committed baseline. A group message legitimately has no recipient,
and an imported call may have an unlinked party. Participant label formatting
unconditionally reads each participant's case. The shared Findings list and
individual result both call it when rendering AI findings, so a supported
source can make the entire page return HTTP 500. A synthetic group claim passed
the claims validator with a mocked model response and then raised
`AttributeError` during rendering. Handle absent links while retaining the
existing same-case check. Cover both affected views.
[Formatter](../../core/analysis/presentation.py#L52) ·
[Group model](../../phone/models.py#L113) ·
[Findings list](../../core/analysis/catalogue.py#L463) ·
[Saved result](../../core/analysis/findings.py#L301)

### AUDIT-002 — Case Chat can use evidence outside an explicit turn scope

**Origin** Committed baseline. The initial hosted search and source-following
tool apply investigator-entered `type:`, `on:`, `from:`, and `through:`
filters. Prior-turn hints and reply context, report discovery, and
relationship leads can use sources from
outside those filters. The model can receive this material and use it in an
answer that appears to honor a restricted scope. This occurs within the same
case and approved model route. Apply the current turn's source rules to every
context path and again at publication. Cover a scoped follow-up whose previous
answer or linked source falls outside the new scope.
[Scope](../../core/evidence_chat/search_scope.py#L50) ·
[Model context](../../core/evidence_chat/investigation.py#L205) ·
[Reports and leads](../../core/evidence_chat/investigation.py#L415) ·
[Prior-turn hints](../../core/evidence_chat/investigation.py#L481)

### AUDIT-003 — Whole-person writing runs have no end-to-end resource budget

**Origin** Pending. Submission enumerates every eligible message under the case
write lock, bypasses the generic source/material limits, and builds and stores
the full input manifest. Worker batching limits most reads, but a single large
message can exceed the batch target and the SQLite aggregates grow with the
number of distinct words and phrases. Legitimate large histories can tie up the
case lock and exhaust web or worker memory, database storage, or scratch space.
Freeze membership in bounded background batches, retain exact selectors and
source hashes, and make resource exhaustion a recoverable outcome.
[Submission](../../text/writing_views.py#L165) ·
[Source enumeration](../../text/writing_views.py#L98) ·
[Limit bypass](../../core/analysis/services.py#L109) ·
[Batching](../../text/analysis/attributed_sources.py#L83) ·
[Scratch storage](../../text/analysis/attributed_writing.py#L536)

## P2

### AUDIT-004 — Paged saved writing results still rehash the entire source corpus

**Origin** Pending. Each detail request, including navigation between source
pages, validates every message before returning the visible slice. The Important
view and download path also call whole-manifest content verification. Batching
bounds individual reads while total CPU and I/O still scale with corpus size on
each request. Preserve byte-level checks using evidence revision-bound
verification and read the requested source page separately.
[Detail and paging](../../core/analysis/run_views.py#L71) ·
[Important](../../core/analysis/important.py#L74) ·
[Download](../../core/analysis/output_views.py#L25) ·
[Message hashing](../../core/analysis/source_resolution.py#L261)

### AUDIT-005 — Search and Case Chat accept a weaker integrity check for writing reports

**Origin** Pending. Saved report material checks attributed message membership
with content loading disabled. The source verifier then skips recomputing the
message's retained original checksum. If corruption or an out-of-band write
changes stored message bytes while their recorded digest stays the same, the
result detail and export can withhold the result while search and Case Chat
continue serving the stale report.
Require the same integrity standard across readers, with verification that
scales for large manifests.
[Report material](../../core/evidence_chat/analysis_reports.py#L101) ·
[Checksum verification](../../core/analysis/source_resolution.py#L261) ·
[Hosted indexing](../../core/case_search/material.py#L103) ·
[Citation reopening](../../core/evidence_chat/corpus.py#L752)

### AUDIT-006 — Date-only exports can become exact event times

**Origin** Pending writing path and committed communication paths. Some imports
store midnight as a placeholder for a source calendar date. Person Writing
filters by that timestamp in the account timezone and treats it as a reported
event time. The account timezone may place the message on the previous day, and
the comparison can publish a change interval with a fabricated time boundary.
Conversation grouping and structured message queries also consume placeholder
times as timestamps. A synthetic timezone conversion and writing comparison
produced both the prior-day shift and timestamped interval. Reuse the source
clock classification, distinguish the reported date from an event instant, and
suppress unsupported interval bounds.
[Source clock](../../core/evidence/source_clocks.py#L27) ·
[Writing selection](../../text/writing_views.py#L64) ·
[Interval publication](../../text/analysis/attributed_writing.py#L465) ·
[Session grouping](../../phone/grouping_jobs.py#L243) ·
[Chat counts](../../core/evidence_chat/engine_counts.py#L159)

### AUDIT-007 — One quotation-dense message can stop an entire writing run

**Origin** Pending. The person workflow applies automatic quotation exclusions
to every message. It inherits a 200-span cap from the generic method, with no
per-message selection or exclusion control to correct the blockage. A synthetic
message passed at 200 detected quotations and raised `ValidationError` at 201.
Bound the detection while allowing an explicit, accurately described recovery
path. Test that exclusions, source coverage, and any omissions remain visible.
[Person sampling](../../text/analysis/attributed_writing.py#L36) ·
[Exclusion cap](../../text/analysis/deterministic.py#L254) ·
[Person form](../../text/templates/text/person_writing.html)

### AUDIT-008 — Failed hosted indexes can retain archived passages indefinitely

**Origin** Committed baseline. Archiving dirties the affected search source and
marks the index for reconciliation. If the active index has exhausted retries
on a transport or configuration failure, neither hook queues work. Previously
uploaded passages can remain in the private provider index after service
recovers, until an editor or operator intervenes. The local canonical checks
continue to reject the archived source, but the old remote text can still
consume ranked slots. Give withdrawal changes a recoverable cleanup path that
does not depend on the active index completing unrelated uploads.
[Archive hook](../../core/case_search/indexing.py#L22) ·
[Queue conditions](../../core/case_search/lifecycle.py#L123) ·
[Remote passage removal](../../core/case_search/lifecycle.py#L154) ·
[Local verification](../../core/case_search/hosted.py#L153)

### AUDIT-009 — A malformed provider receipt escapes indexing recovery

**Origin** Committed baseline. The receipt validator checks status with a set
membership test before checking its type. A JSON array or object triggers an
uncaught `TypeError`, rolling the delivery back and leaving the index locally
queued after the underlying task fails. A synthetic call confirmed the exception.
Validate field types before membership checks and cover recovery from a
wrong-typed upload or poll receipt.
[Receipt validation](../../core/case_search/lifecycle.py#L202) ·
[Recovery handler](../../core/case_search/lifecycle.py#L479)

### AUDIT-010 — Cross-case search has a per-call timeout with no page deadline

**Origin** Committed baseline, still present in the updated results page. A
single request searches up to eight cases serially. Each search makes two
provider privacy checks and a query with a 15-second transport timeout per
exchange. Slow calls can make one case delay all later cases and prevent already
completed groups from rendering before the browser or proxy gives up. Use a
total request budget or independently load bounded result groups while retaining
current per-case authorization, privacy checks, and rate accounting.
[Serial case loop](../../core/case_search/views.py#L166) ·
[Provider privacy checks](../../core/case_search/cloudflare.py#L126) ·
[Transport timeout](../../core/case_search/cloudflare.py#L184)

### AUDIT-011 — Conversations can load hundreds of MiB for a single page

**Origin** Committed baseline. The conversation timeline and expanded session
paginate by message count and fetch and render each selected body in full.
A page can contain 50 message bodies, each potentially near the 10 MiB retained
text limit. Database transfer, HTML rendering, and browser parsing can exhaust
memory or time out on valid large evidence. Bound bytes on list pages and keep
complete text available from the focused original reader.
[Message input limit](../../core/evidence/forms.py#L39) ·
[Timeline query](../../phone/conversation_views.py#L126) ·
[Timeline template](../../phone/templates/phone/conversation_stream.html#L20) ·
[Session query](../../phone/conversation_instances.py#L605)

### AUDIT-012 — Unrelated evidence updates can invalidate a finished writing run

**Origin** Pending. Publication reads the case-wide evidence revision,
revalidates all selected bytes, and compares the revision after taking the
publication lock. A concurrent unrelated evidence write during that scan
causes the job to fail after it has computed the result. This window grows
with the manifest size. An existing test demonstrates failure and manual
retry after an unrelated original is added. Confirm the exact selected
dependencies at publication and allow bounded revalidation of unrelated
revision changes.
[Publication](../../core/analysis/worker.py#L198) ·
[Existing test](../../text/tests/test_writing_views.py#L211)

### AUDIT-013 — Repeated writing phrases can cite an unrelated original

**Origin** Pending. Counts normalize three successive words across punctuation
and whitespace, while profile citation lookup searches for the normalized
phrase as a literal string. A synthetic phrase repeated with punctuation
counted correctly and produced no anchors. The finding then defaults to its
sample's first original, which need not contain the phrase. Use the same
offset-producing tokenization for counts and examples, as the comparison
already does.
[Phrase counting](../../text/analysis/attributed_writing.py#L51) ·
[Finding fallback](../../text/analysis/attributed_writing.py#L217) ·
[Example lookup](../../text/analysis/attributed_writing.py#L244) ·
[Comparison anchors](../../text/analysis/attributed_writing.py#L403)

### AUDIT-014 — Full literal review can miss eligible text at a value boundary

**Origin** Committed baseline. The Worker scans the entire rendered record
with non-overlapping matches and discards results outside eligible ranges.
A rejected match overlapping a generated field label and a retained value
can consume the start of an otherwise eligible match. A synthetic Worker probe
returned an empty result for an exact value. Django checks only the supplied
offsets, so the omission becomes a completed miss. Match all eligible starts
while preserving offset and word-boundary checks.
[Worker matching](../../cloudflare/investigator/literal.js#L32) ·
[Offset verification](../../core/evidence_chat/full_review.py#L193)

### AUDIT-015 — Journey comparison offers findings that cannot fit its input limit

**Origin** Committed baseline exposed by the pending eligibility UI. The
picker flags automatic trips over the source budget. A retained visit or journey
finding can still appear selectable even when it expands to every input of a
previous run that used the full limit. Adding the mandatory statement makes
the review impossible. Check the effective source budget for both derived
types and preserve the previous run's provenance when reducing expansion.
[Derived picker](../../geo/journey_claim_inputs.py#L97) ·
[Prior run expansion](../../geo/journey_claim_inputs.py#L211) ·
[Source budget](../../geo/journey_claim_requests.py#L57)

### AUDIT-016 — Conversation paging can silently skip retained messages

**Origin** Committed baseline. Infinite scrolling appends pages read from
fresh offsets in the mutable active message set. If an earlier message is
archived between reads, the next page shifts forward over a still-active
message. The client deduplicates IDs without detecting the gap or removing
the now-archived row. Use a stable boundary or check the conversation
revision before appending. Cover archive and restore while a reader is open.
[Server paging](../../phone/conversation_views.py#L148) ·
[Active message selection](../../phone/conversations.py#L27) ·
[Client append](../../phone/static/phone/conversations.js#L145)

### AUDIT-017 — Case Chat can retrieve legacy investigator notes

**Origin** Committed baseline. The Case Search index includes the legacy note
fields on person, vehicle and point-of-interest records. The Case Chat wrapper
opts in to the same record passages, and its context builder reopens them for the
model. A local synthetic record confirmed that the excerpt contains the note.
This can turn investigator notes into cited material despite the requested
notes exclusion for Chat. Give Chat its own retrievable source
policy, preserve the legacy notes, and check that an eligible record field can
still be cited without sending the note from the same record.
[Indexed record fields](../../core/case_search/material.py#L16) ·
[Chat retrieval](../../core/evidence_chat/investigation.py#L127) ·
[Record opt-in](../../core/case_search/hosted.py#L178) ·
[Case Search documentation](../cases.md#external-ai-and-hosted-search)

## P3

### AUDIT-018 — Invalid report filters skip error-summary focus

**Origin** Pending. An invalid report filter response returns HTTP 200 and is
swapped into the current page. The partial-navigation handler focuses the first
section heading or summary. It bypasses the normal enhancement that focuses
the linked error summary. A synthetic Chromium probe confirmed focus on
`report-filters` while the error summary was visible. Apply the error-summary
behavior after a partial form response and cover invalid input with the
keyboard.
[Partial navigation](../../ui/static/ui/workspace.js#L135) ·
[Normal error focus](../../ui/static/ui/app.js#L116) ·
[Report response](../../core/analysis/report_views.py#L116)

### AUDIT-019 — Processing can refresh saved call-import rows without a request

**Origin** Committed baseline. The history region's populated marker considers
other history types while omitting call imports rendered inside the region.
When calls are the only history, a background refresh can replace the rows
without the investigator choosing to show updated processing history. Derive
the marker from all rendered histories and cover a call-import-only case.
[Region marker](../../core/templates/core/analysis/processing.html#L215) ·
[Call imports](../../core/templates/core/analysis/processing.html#L239) ·
[Refresh decision](../../ui/static/ui/live.js#L235)

### AUDIT-020 — Refresh saved results can lose the investigator's request on retry

**Origin** Committed baseline. Refresh consumes the force flag before the status
request. If that request fails, the error handler restores the saved-results
intent, but the retry's fragment predicate ignores it. A synthetic Chromium
probe confirmed the older rows stay displayed after a status failure and
successful unchanged retry. Carry the intent through retries until the fragment
is applied or the investigator leaves the page.
[Refresh flags](../../ui/static/ui/live.js#L189) ·
[Fragment predicate](../../ui/static/ui/live.js#L217) ·
[Retry handler](../../ui/static/ui/live.js#L264)

### AUDIT-021 — Editing during source browsing can silently cancel navigation

**Origin** Pending. While source paging or search is in flight, an edit makes
the handler discard its response and skip fallback. A second browse attempt is
also ignored while the picker is busy. The investigator can stay on the old page
without feedback after asking for the next page or a new search. Preserve edits
and announce a cancelled action, or apply the latest pending browse request.
Chromium confirmed that a delayed request followed by an edit and a second
Next action stayed on the first page with no announcement.
[Busy and edit guards](../../ui/static/ui/workspace.js#L172) ·
[Fallback](../../ui/static/ui/workspace.js#L211)

### AUDIT-022 — Some saved exclusion examples cannot open their original

**Origin** Pending. A period's comparison output stores nested exclusion offsets,
but its source references are derived from usable coverage samples. The source
resolver reads top-level finding references and anchors. A wholly excluded
message that is absent elsewhere can therefore have a saved exclusion without
a source link or readable character position. Resolve and validate the nested
references against the exact manifest while keeping example counts bounded.
[Comparison output](../../text/analysis/attributed_writing.py#L439) ·
[Reference collection](../../text/analysis/attributed_sources.py#L118) ·
[Example display](../../core/analysis/presentation.py#L284)

### AUDIT-023 — Saved writing comparisons omit their selected periods and timezone

**Origin** Pending. The run reader removes `writing_scope` from displayed
settings and surfaces the person and selected source count. When an investigator
opens the saved result directly from Processing, Group A and B show observed
coverage without the selected calendar windows or interpretation timezone.
Show the frozen selection on the result alongside the measured coverage.
[Run settings](../../core/analysis/run_views.py#L204) ·
[Result template](../../core/templates/core/analysis/detail.html#L107) ·
[Person history](../../text/templates/text/person_writing.html#L64)

### AUDIT-024 — A failed live writing count can leave a stale number visible

**Origin** Pending. The count request intentionally ignores aborted responses
when a new date selection supersedes the old one. Network failures and
timeouts can return the same status code, so the old count remains under the
new dates with no indication that it is stale. A synthetic Chromium probe
confirmed this state. Mark the count pending on edits and distinguish a
superseded request from a transport failure.
[Failure handler](../../ui/static/ui/workspace.js#L282) ·
[Writing form](../../text/templates/text/person_writing.html#L40)

### AUDIT-025 — The Processing browser test still targets a removed disclosure

**Origin** Pending. The Processing history filters now render directly as a
form. The existing browser test waits for a nested `details` element before
exercising refresh, focus, or draft preservation. Chromium timed out at that
locator in both the full run and the focused rerun. The two-engine test also
leaves the first engine's queued fixture in place when it aborts early, so the
second engine can fail at its initial count. Select the current form by its
accessible name and isolate the fixtures per engine.
[Current filters](../../core/templates/core/analysis/processing.html#L198) ·
[Test flow](../../ui/tests/live_browser.py#L301) ·
[Stale selector](../../ui/tests/live_browser.py#L324)

## Validation

- `make static-check` passed repository hygiene, Ruff lint and format, lockfile
  validation, and Compose configuration. JavaScript syntax checks passed for
  the four changed workspace scripts.
- Fresh PostgreSQL migrations, Django database checks, and migration drift
  checks passed under synthetic settings. `make test` completed 2,009 tests
  with one skip. The CI classification, deployment, release and smoke helper
  suite passed 135 tests.
- `make compose-smoke` passed against a disposable project, including startup,
  private originals, static delivery, PDF and audio handling, and worker
  restart. The Cloudflare Worker passed five local tests and a Wrangler
  bundle dry run.
- The first browser invocation used an artifact directory outside the
  harness's permitted path. After correcting it, `make browser-check` ran 163
  tests in Chromium and WebKit and reported two failures and one error.
  The Processing test accounted for one failure and one error, both repeated
  in a focused rerun (AUDIT-025). The other failure was a WebKit conversation
  search highlight outside the viewport. The complete conversation journey
  passed on both engines in isolation. [Viewport assertion](../../phone/tests/conversation_browser.py#L875)
- Focused synthetic probes exercised nullable participants, date-only
  timestamps, writing citations and exclusions, malformed search receipts,
  note excerpts, literal review, and browser focus and refresh behavior.

All functional validation used local synthetic data. Hosted CI and live
provider behavior are outside this snapshot.

## Astra 6.2 Ultra recheck

Three GPT-6.2 Astra reviewers, each configured for Ultra reasoning, checked
all 25 findings against a frozen copy of the source captured at 10:06 a.m. EST
on 27 November 2026. HEAD was still `3117b330`. The source manifest was
`1e6b94295a60739fd5653bdbb303b01eb4a0883f74a398d9c09b8253dde1519b`.
The worktree changed between the two snapshots. These dispositions account for
changes already present in the later source.

**Result** 19 remain open, including two P1s. Three are resolved or superseded
for the reviewed workflow. Three now have narrower remaining scope.

| Finding | Disposition | Recheck |
| --- | --- | --- |
| AUDIT-001 | Open · P1 | A synthetic group-message claim passed validation; formatting its absent recipient raised `AttributeError`. A results page fails when it renders an affected finding. |
| AUDIT-002 | Open · P1 | In an ordinary, explicitly filtered turn, prior-turn context and the report and relationship tools can bypass the filter inside the same approved case. Full Review applies its scope separately. |
| AUDIT-003 | Superseded for new runs | Selection and source snapshotting now cap a run at 10,000 messages and 16 MiB. Request work under the case lock still scales to those bounds. [Selection](../../text/writing_views.py#L108) · [Limits](../../text/analysis/attributed_sources.py#L47) |
| AUDIT-004 | Open · P2 | Detail, Important and export still rehash the complete original set. The new caps bound the cost of newly submitted runs. [Result reader](../../core/analysis/run_views.py#L88) |
| AUDIT-005 | Partial · P2 | The metadata-only path predates the pending work. Version 1 attributed reports now verify original message bytes. Legacy writing reports still use the metadata-only path, relevant when content changes outside normal saves while its stored checksum stays unchanged. [Both report paths](../../core/evidence_chat/analysis_reports.py#L101) |
| AUDIT-006 | Partial · P2 | The timestamp issue, including in Person Writing, predates the pending work. Updated Person Writing excludes date-only messages from dated selections and treats them as untimed in comparisons. Generic writing, conversation grouping and structured Chat counts still use the placeholder instant. [Writing selection](../../text/writing_views.py#L70) · [Comparison](../../text/analysis/attributed_writing.py#L544) |
| AUDIT-007 | Open · P2 | The 200-exclusion limit also affected Person Writing at HEAD. Both generations accepted 200 quotation spans and rejected 201 in local probes. |
| AUDIT-008 | Open · P2 | A failed index with exhausted transport retries still requires recovery to remove archived passages remotely. Local canonical reads reject archived material. [Retry and reconciliation](../../core/case_search/lifecycle.py#L128) |
| AUDIT-009 | Open · P2 | A wrong-typed receipt status can still escape controlled delivery recovery. The recent polling and scheduling changes leave the status check intact. [Current check](../../core/case_search/lifecycle.py#L206) |
| AUDIT-010 | Open · P2 | The revised shared rate limit leaves up to eight searches serial, with per-exchange timeouts and no request-wide deadline. |
| AUDIT-011 | Open · P2 | Both conversation readers still load full bodies. The 500 MiB figure is a calculated upper bound on raw body data per page. |
| AUDIT-012 | Open · P2 | The case-wide revision guard can prevent publication of a computed result after an unrelated write during final verification. The existing test confirms that the exact frozen selection succeeds on retry. [Guard](../../core/analysis/worker.py#L198) |
| AUDIT-013 | Resolved | The profile now uses the same tokenizer for counts and source anchors. The punctuation probe returned both exact offsets and cited only the original containing them. [Current example extraction](../../text/analysis/attributed_writing.py#L234) |
| AUDIT-014 | Open · P2 | A local Worker matcher probe confirmed a rejected overlap can still consume the start of an eligible literal match. |
| AUDIT-015 | Open · P2 | The derived-journey picker can offer a selection whose prior-run sources, plus the required statement, exceed the distinct-source limit. |
| AUDIT-016 | Open · P2 | A synthetic paginator probe confirmed one active row was skipped after an earlier row was archived between page reads. |
| AUDIT-017 | Open · P2 | Chat still retrieves legacy entity note fields. The prior Chat exclusion appears in the committed interface guide at `3117b33:docs/interface-design.md:397`. The probe exercised legacy notes on entity records. |
| AUDIT-018 | Open · P3 | Chromium and WebKit focused the disclosure while the invalid-report error summary was visible. |
| AUDIT-019 | Open · P3 | Both engines refreshed saved call-import rows without an explicit request in a call-import-only history. |
| AUDIT-020 | Open · P3 | Both engines lost an explicit refresh after an initial status failure and an unchanged successful retry. A second click can recover. |
| AUDIT-021 | Open · P3 | Both engines discarded an in-flight browse on a picker edit, preserved the edit, and left the user on the old page without an announcement. |
| AUDIT-022 | Partial · P3 | Newly produced comparisons include the missing anchors. A reconstructed result in the earlier uncommitted format still omits the links. This residual applies only if that earlier format was used to save results. [Current producer](../../text/analysis/attributed_writing.py#L451) |
| AUDIT-023 | Open · P3 | Direct report views still omit the chosen date windows and timezone. Person history and result downloads expose both. |
| AUDIT-024 | Open · P3 | Both engines retained a stale count after a date change and an XHR transport failure. |
| AUDIT-025 | Resolved | The test selects the accessible filter form and isolates each engine's fixture. The corrected Processing journey passed the pinned browser rerun. [Test](../../ui/tests/live_browser.py#L302) |

The frozen snapshot passed 102 focused Django tests against disposable
PostgreSQL, including the Chromium and WebKit Processing and conversation-search
journeys. The independent reviewers passed 40 database-free Django tests and
five Worker tests. Five focused UI probes used synthetic intercepted responses
in both engines. An initial live-tree test caught a transient generator
handling error during the edits. The pinned run passed that test.

## Remediation after the Astra recheck

The table records changes made for the 19 open findings and the three partially
resolved findings. It also records the three fixes already present in the
rechecked source.

| Finding | Resolution |
| --- | --- |
| AUDIT-001 | Findings and saved results render supported group messages and calls with unlinked participants. |
| AUDIT-002 | Case Chat applies the current turn's explicit scope to search, source references and publication. It withholds inherited prose and reports whose scope it cannot establish. |
| AUDIT-003 | Existing selection limits cap new writing runs at 10,000 messages and 16 MiB. |
| AUDIT-004 | Saved writing reports reuse successful original-byte verification for the same case evidence revision and checksummed input scope. |
| AUDIT-005 | Legacy writing reports also verify original bytes before search indexing, citation or display. |
| AUDIT-006 | Generic writing and session methods treat date-only messages as untimed. Chat context and conversation readers show the source calendar date. Dated Chat counts disclose uncertainty. |
| AUDIT-007 | A writing sample with too many automatic quotation candidates is recorded as an excluded original. Remaining eligible messages can still be analyzed. |
| AUDIT-008 | Archiving queues index cleanup even after retry exhaustion. Deletion and rechecking of indexed derivatives take precedence over unrelated upload work. |
| AUDIT-009 | Wrong-typed provider receipt statuses enter the normal recovery path. |
| AUDIT-010 | Cross-case search uses a shared eight-second provider lookup budget and retains groups already verified. |
| AUDIT-011 | Transcript, message panel and session readers bound message previews in the database. The retained original remains available. |
| AUDIT-012 | Publication rechecks the frozen selection under the case lock after another evidence write. It publishes only if the selected originals still verify. |
| AUDIT-013 | Existing phrase counts and example citations use the same tokenizer. |
| AUDIT-014 | The literal matcher considers eligible starts that overlap rejected candidates. |
| AUDIT-015 | The journey comparison picker excludes derived choices that would leave no input slot for the required statement. |
| AUDIT-016 | Incremental transcript paging detects changes in active membership and requires a refreshed transcript. |
| AUDIT-017 | New Chat turns omit legacy investigator notes. Published history stays readable. |
| AUDIT-018 | Invalid report filters focus the error summary. |
| AUDIT-019 | Call-import-only histories wait for the investigator to refresh saved rows. |
| AUDIT-020 | The investigator's refresh request survives a temporary status failure. |
| AUDIT-021 | An edit that interrupts picker navigation keeps the draft and announces how to retry. |
| AUDIT-022 | Older exclusion examples reopen their exact available original alongside examples in the current format. |
| AUDIT-023 | Direct writing reports show their saved selection dates and timezone. |
| AUDIT-024 | Writing counts indicate pending or unavailable after a date change and recover on a later successful request. |
| AUDIT-025 | The existing Processing test uses the accessible filter form and a separate fixture for each engine. |

The operating guides describe [cross-case search and cleanup](../cloudflare-search.md),
[Chat scope](../cloudflare-agent.md), [writing and conversation analysis](../analysis-communications.md),
[conversation browsing](../evidence.md#reading-conversations), and
[live updates and source selection](../interface-design.md#processing-and-refresh).

The isolated remediation passed `make check` against disposable PostgreSQL.
Django ran 2,040 tests with one skipped. The fresh migration also applied,
reverted and reapplied successfully. Eighteen focused browser journeys passed
in Chromium and WebKit. All six Worker tests and the local Wrangler dry-run
bundle passed. Browser fixtures were synthetic and provider responses were
mocked.

After the simplicity review, the result reader kept the view's single
publication snapshot, and dated Chat counts classified the reported clock in
one query. The affected Django suites passed all 549 tests. The saved writing,
report browsing and Case Chat browser journeys passed again in both engines.
