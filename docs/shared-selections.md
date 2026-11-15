# Shared selections and briefs

Open **Case tools → Selections** to create named collections of exact retained
sources and individual findings. Owners and editors create, rename, add/remove
members, and delete selections. Viewers inspect and export. Every request checks
current case access; a selection cannot grant access to another case.

A selection has at most 500 members. It stores references, not copies of original
content, analysis text, or titles. Source references preserve original and optional
call-addition IDs, checksums, exact extraction result IDs, and accepted geocode
candidate/review references. Finding references identify the immutable result and
zero-based item index. Selecting a finding does not accept or verify it.

## Saving and reusing material

Use **Save to selection** in an evidence reader's More menu, on a retained call
addition, or beside an individual finding. Choose an existing selection or create
one. A selection's **Add sources** view searches the evidence collection; after
choosing an original, **Other retained versions** offers bounded retained
extraction/transcript choices. The selected version is never replaced by a newer
result. Duplicate exact references are ignored.

Collection edits check a signed revision under the case/selection lock. A stale
rename preserves the proposed name and shows the current saved name. Stale
membership/removal/deletion submissions require a refreshed review. Deleting a
selection removes only its references; original evidence, results, and reviews
remain retained.

**Analyze sources** offers ordinary analysis for source-only selections, with
current integrity, eligibility, configuration, and approval checks. Incompatible
members are not silently dropped. Selections containing findings use **Ask a
question** instead. A changed geocode approval requires reviewing the retained
selection before ordinary analysis rather than silently rewriting its snapshot.
Archived evidence remains usable.

## Portable brief

**Download brief** generates one standalone printable HTML file with its exact
JSON provenance manifest embedded. The file's **Download provenance manifest**
control saves those same embedded bytes. There are no stored export artifacts or
remote assets. Browser print is available; Pandect does not generate a PDF or
bundle original files.

The brief records the case, selection ID/revision, generation time, exact source
references/checksums, original attribution, cited anchors, method/version and
execution provenance, current review decisions/reasons/reviewer/times, retained
location approvals, and partial-coverage warnings. Synthesis findings preserve
exact prior-finding quotations and their result/item/character references, with
underlying original attribution shown separately. Original and finding excerpts
are limited to 4,000 characters and label their character range and truncation.
An export is limited to 16 MiB; larger exports require a smaller selection.

Reviews shown in the application are current. The generated brief freezes the
review state at export time, and remains readable independently of later app
changes. Rejected and unresolved findings retain those labels. Whole-result
review remains distinct from individual finding review.

All required sources are revalidated, including every contributing input to a
selected result. Missing, deleted, changed, cross-case, or otherwise unavailable
members block export with an actionable member list. No selected material is
silently omitted. Newer geocode review is disclosed while historical inspection
preserves the selected approval. Synthesis findings also validate their retained
prior-finding dependencies.

Individual evidence deletion inventories include affected selection references
and collection revisions. After confirmed deletion, references remain as
unavailable placeholders; copied deleted content is never retained in selections.
Changed relevant membership invalidates the deletion confirmation. Whole-case
deletion removes its selections.
