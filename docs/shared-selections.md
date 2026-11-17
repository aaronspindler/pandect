# Shared selections and briefs

**Case tools → Selections** creates named collections of exact retained sources
and findings. Owners/editors create, rename, add/remove members, and delete;
viewers inspect/export. Every request checks current case access; selections
cannot grant access across cases.

Each selection holds at most 500 references, without copying original content,
analysis text, or titles. Source references retain original/optional call-addition
IDs, checksums, exact extraction result IDs, and accepted geocode candidate/review
references. Findings identify immutable results and zero-based item indexes;
selection does not accept or verify a finding.

## Saving and reusing material

Choose **Save to selection** beside the evidence reader's original, on a call addition,
or beside a finding. Select/create a collection. **Add sources** searches Evidence;
after choosing an original, **Other retained versions** offers bounded extraction/
transcript choices. Newer results never replace selected versions. Exact duplicate
references are ignored.

Edits check signed revisions under the case/selection lock. Stale renames preserve
the proposed name beside the saved value; stale membership/removal/deletion needs
refreshed review. Deleting a selection removes references only, retaining originals,
results, and reviews.

**Analyze sources** supports source-only selections with current integrity,
eligibility, configuration, and approval checks. Incompatible members are never
silently dropped. Selections containing findings use **Ask a question**. Changed
geocode approval requires selection review before ordinary analysis; snapshots
are never silently rewritten. Archived evidence remains usable.

## Portable brief

**Download brief** creates standalone printable HTML with an embedded exact JSON
provenance manifest. **Download provenance manifest** saves those same bytes.
There are no stored exports or remote assets. Browser printing is supported;
PDF generation and original-file bundles are not.

Briefs record case, selection ID/revision, generation time, exact source references/
checksums, attribution, cited anchors, method/version, execution provenance,
current review decisions/reasons/reviewer/times, location approvals, and partial
coverage. Synthesis findings retain prior-finding quotations and result/item/
character references, separately from underlying original attribution. Original/
finding excerpts cap at 4,000 characters with ranges/truncation labels. Exports
cap at 16 MiB; larger exports require smaller selections.

The app shows current reviews; briefs freeze export-time reviews and remain
readable independently of later changes. Rejected/unresolved labels remain, and
whole-result review stays distinct from individual review.

All required sources, including every contributing result input, are revalidated.
Missing, deleted, changed, cross-case, or unavailable members block export with
an actionable list; nothing is silently omitted. Newer geocode review is disclosed
while historical inspection preserves selected approval. Synthesis also validates
retained prior-finding dependencies.

Evidence-deletion inventories include affected references/revisions. Confirmed
deletion leaves unavailable placeholders without copied content. Changed relevant
membership invalidates confirmation; whole-case deletion removes selections.
