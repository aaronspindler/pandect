# Cases and collaboration

`cases.Case` is the investigation/access boundary: every evidence record requires
a case. `cases` owns collaboration, invitations, settings, authorization, and
navigation; `core` owns Evidence, Processing, and Case Tools; `geo` owns maps/POIs.

## Investigator workflow

With a verified account, choose **All cases → Create case** and enter a name.
The creator becomes owner. Opening, creating, or joining a case checks access
and opens **Case Tools**, as do saved case URLs. Shared navigation links to
**Case Tools**, **Evidence**, and **Processing**. On the homepage, **Explore
evidence**, **Case records**, and **Reports** link directly to the case
workspaces and each report. Use **Jump to latest records** to see recently
added evidence and newly saved findings together, newest first. Open a title
to read the original or finding.

Owners and editors can **Add evidence** or jump to **Analysis methods** and
choose a method to set up. Owners can open **Case settings** from the header.
**Case details** shows the owner, your role, creation and update times, and any
saved read-only description. Navigation labels stay visible on desktop and mobile.
All cases searches accessible names/descriptions and filters owned/shared cases.

**Search this case** at the top of each case screen finds evidence, people,
vehicles, conversations, places, saved trips and findings. Results link to
their sources. For example, paste a full evidence identifier to open its record
or use a quoted phrase to find it in a retained transcript. With meaning search
enabled, choose **Search by meaning** to find passages that use different wording.
See the [investigator interface](interface-design.md) for shortcuts and filters.

Viewers inspect material and download originals. Editors also add/edit records
and archive/restore evidence. Owners manage access and delete people, vehicles,
or the case, subject to retained-evidence protections. Product users need no staff
status or model permissions. See [maps](maps.md) for Map coverage and privacy.

### Important items

Owners and editors can choose **Mark important** in an evidence reader's
**More** menu, on an individual finding, or on a whole Evidence chat reply. **Case Tools → Important** lists
the case's marked items, newest mark first. Open the title to revisit the exact
record, finding, or reply. **Back to Important** returns to its list page.
Finding summaries keep their original labels and show the current review outcome
for sighting proposals.

Open **Notes** to read dated entries and see who added each one. Owners and
editors can add entries while the item is marked. Viewers can read the list
and notes. Marking evidence and returning to it from **Notes** retain the active
Evidence search, filters, and page. **Remove from Important** hides the item
from the list. Its notes remain accessible from the source, and marking it
again restores the list entry.
Notes are investigator annotations. They are separate from evidence, findings,
and review decisions. Evidence chat excludes them from retrieval and model prompts.

Archiving evidence retains its Important mark and notes for restoration. It hides
the evidence and its notes from Important, and withholds findings and replies
that depend on archived sources. Replacing a finding in current results keeps the
mark on the saved item. Findings and replies with unavailable sources are withheld.
Permanent evidence deletion removes affected marks and
notes as part of the reviewed inventory. Deleting the case also removes its
marks and notes.

## Case settings

Under **Prepare conversations and documents for search**, the owner can review
estimated work before separately enabling conversation summaries and document or
transcript briefs. The estimate counts eligible saved conversations, messages,
sources and sections. Background preparation queues missing work against the
selected AI service. New groupings and retained text or extractions are checked
as they arrive. **Check for missing work** also revisits recoverable failures.
An external service requires a current case approval for its destination.
Workers recheck the selected destination, model and standing approval. A changed
service requires a fresh owner review to use it for generation. Disabling generation stops subsequent requests and publication.
Previously completed suggestions retain their history. Search can use them while
their exact underlying originals remain accessible.

Settings leads with collaborators and invitations. Owners open **Rename case**
to edit the name. Stale edits reopen the form and show the draft beside newer
values for **Save reviewed changes**.
**Delete this case → Delete case** opens a named confirmation listing affected
records, including archived evidence. There is no in-app undo; user accounts
remain. See [operations](operations.md) and [evidence](evidence.md) for storage
and backup limits.

## AI backend

The case owner chooses **Local AI** (the default) or **External AI** under
**Case settings → AI processing**. The selection applies to new automatic checks,
evidence reprocessing, and on-demand AI checks without a dedicated service.
Each check uses the selected backend when available. Choose the processing
location and models together, then use **Save changes**. Pandect opens a review
when the selection enables a new external destination. Built-in checks and
geographic services have their own requirements.

Changing the selection applies to new requests. Queued requests and saved
results keep their original service. Revoke external approval to stop subsequent
requests to approved external destinations, including requests from queued work.

## External analysis approval

Under **Case settings → AI processing**, choose a model for each task or
keep its installation default. **Save changes** applies all assignments together.
On first use of a destination, review the provider that receives the material,
the gateway where applicable, and the transfer and retention details.
**Approve and save** applies the reviewed settings. Cancelling keeps the
previous settings.

Text approval covers the six listed text tasks, including Evidence chat
questions, necessary follow-up context, relevant evidence excerpts and source
references. Sharing recording chunks requires its own approval. Switch models
freely within an approved destination and data scope. A change to the receiving
service, gateway identity or transfer terms opens a new review. Earlier
model-specific approvals continue to cover the settings originally reviewed.

Use **Manage** beside the approval summary to inspect or revoke access. Each
outbound request rechecks permission. A queued check whose model has changed
stops before its next send. Start another attempt to use the new assignment.
Revocation stops subsequent transfers. Provider retention applies to requests
already sent.

New cases start without external approval. Owners and editors can start
**Reprocess evidence** for retained material.

## Hosted meaning search

Under **Case settings → Meaning search**, the verified owner reviews the Cloudflare
account and gateway, OpenAI key alias and embedding model. **Approve and start
indexing** queues eligible active passages and covers future additions and
restorations. The service also receives the text of queries that case members
explicitly submit with **Search by meaning**. Keyword searches stay in Pandect.

**Processing → Meaning-based search** shows indexing progress and how to resume stopped
work. Changing the embedding destination requires a fresh approval. **Revoke
meaning search approval** stops subsequent transfers, including queued work.
Revocation preserves retained evidence. Search approval is independent of the
case’s AI backend and external analysis approval.

With a current meaning-search approval, the owner can also enable hosted
meaning searches for Evidence Chat. The composer identifies the embedding
destination. Chat can send a short search query derived from the question and
relevant context, including terms from a source or earlier turn used to
resolve a follow-up. Search queries use the existing passage index. Turning
this permission off stops subsequent hosted chat searches and leaves the
case's ordinary meaning-search and indexing approval in place. Earlier
turns keep the transfer scope they had at submission.

## Hosted passage ranking

Under **Case settings → Hosted passage ranking**, the owner can approve the
displayed Cloudflare account, gateway and Workers AI ranking model. Difficult
chat questions and explicitly submitted related searches can send up to 200
characters of a question and 24 candidate excerpts of up to 900 characters
each. Excerpts may contain personal or sensitive case information. The scores
order the originals. Gateway payload logging and cache reuse are disabled.
Review provider retention and configure the gateway accordingly.

This approval is independent of meaning search, background summaries and
the answer model. Revoking or changing the destination prevents subsequent
ranking transfers under the old grant, including queued turns.
[Reranking configuration](local-analysis.md#optional-passage-ranking)

## Account settings

The Account link opens email, password, and timezone settings. Verified secondary
addresses offer **Make primary**; unverified addresses offer **Resend verification**.
Removing secondary addresses requires confirmation. Address changes follow
allauth's verification/authorization rules.
Invalid password-reset links use plain recovery guidance and a direct new-reset
action; valid links retain allauth's password form and confirmation behavior.

Times and date/time fields default to the device's IANA timezone, detected by
JavaScript and stored in the `pandect_timezone` cookie. **Account → Timezone**
saves an account preference across devices, including without JavaScript.
**Automatic** resumes device detection; pages use UTC until a timezone is known.
Labels reflect the date's daylight-saving offset. Ambiguous/nonexistent local
form times require review.

Timezone preferences affect presentation and form interpretation, not stored
instants, originals, or source-export interpretation. Downloads retain recorded
timestamps; analysis grouped explicitly by UTC hour/weekday keeps that basis.

## Invitations

In **Settings → Invite collaborator**, enter an email and choose Viewer (default)
or Editor. Recipients need an account with the invited email verified.
`PANDECT_ALLOW_SIGNUPS` defaults to `true`; closed registration requires an
operator-provisioned account. Invitations do not bypass registration. Acceptance
requires POST and resumes after sign-in/verification; opening a link grants no access.

Links expire after seven days. **Edit role** changes a current member's access.
**Change role and resend** for an invitation sends a replacement link and
invalidates the previous token. Owners can also resend or revoke an invitation.
Mail failure retains an actionable pending invitation: check SMTP before resending.
Pending/failed invitations remain visible; accepted/revoked records appear under
**Invitation history**. `PANDECT_PUBLIC_ORIGIN` supplies email link origins.
Unavailable invitations explain the needed recovery. Email-management and account-
switching actions appear for an invited-email mismatch; expired/revoked links ask
for a new invitation instead.

Records retain token digests. Sessions may retain continuation URLs: treat them
as secrets and never log them. Acceptance rechecks expiry, revocation, verified
recipient, and case state under a case lock. Replay cannot duplicate membership
or restore revoked access; existing roles are preserved. Invitations to owners or
current members are rejected. Membership changes never transfer ownership or add owners.

## Data model

A case has one required owner. Product creation requires a name; trusted ORM/admin
callers omitting it receive `Case #<UUID>`. Descriptions remain read-only. Status
and ownership transfer are unimplemented. [Models](../cases/models.py) own fields
and constraints.

`Case.additional_users` uses `CaseMembership`: required case/user and `viewer`
(default) or `editor` role. PostgreSQL enforces unique case/user pairs and valid
roles. Model validation/admin reject owner membership; no cross-table database
constraint enforces this. ORM callers must call `full_clean()` before saving.

Case deletion cascades to memberships, people, vehicles, and evidence, with
post-commit file cleanup. Deleting a collaborator removes memberships and retains
cases; protected attribution, including uploads/invitations, may block account
deletion. The owner foreign key uses `PROTECT`, including admin/queryset deletion.

## Django admin

The operator console is titled **Pandect administration**.

Open `/admin/` → **Cases**. Scoped administration requires active staff status,
global model permissions, and current case access. Superusers have no access
bypass; membership grants no global permissions.

Anonymous admin sign-in requests redirect to the normal allauth sign-in flow,
including email verification and shared authentication rate limits.

Domain inspection requires `view_<model>` or `change_<model>` and any case role.
Creation requires `add_<model>` and owner/editor access; editing requires
`change_<model>` and owner/editor access. Permission names use the owning app,
such as `people.add_person` or `document.view_documentevidence`. Deletion follows
[people](people.md#django-admin), [vehicles](vehicles.md#django-admin-and-authorization),
or [evidence](evidence.md#permanent-deletion).

Lists, searches, direct/history/deletion URLs, submitted cases, and inline IDs
are scoped server-side. Revocation/downgrade affects subsequent requests in
existing sessions. Records cannot move between cases. Bulk actions are disabled
except for [debug-only superuser evidence deletion](evidence.md#django-admin).
[`CaseScopedAdmin`](../cases/admin_access.py) implements these gates, with the
following case/membership rules:

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| Create | The signed-in user becomes owner | `cases.add_case` |
| List, inspect metadata/history | Owner, viewer, or editor | `cases.view_case` or `cases.change_case` |
| Add collaborator | Owner of saved case | `cases.change_case` and `cases.add_casemembership` |
| Change collaborator/role | Owner of saved case | `cases.change_case` and `cases.change_casemembership` |
| Remove collaborator | Owner of saved case | `cases.change_case` and `cases.delete_casemembership` |
| Delete case | Owner | `cases.delete_case` and contained-model deletion permissions |

Owners need `cases.view_casemembership` or `cases.change_casemembership` to inspect
membership inlines. Assign permissions through user/group admin. Case deletion
also requires permissions for contained records, such as `people.delete_person`
and `vehicle.delete_vehicle`, as listed on confirmation.

Choose **Add case → Save and continue editing**. The server assigns ownership.
Existing case details are read-only in admin. To rename a case, use
[Case settings](#case-settings). The owner-only inline adds existing users,
changes roles, or removes members subject to model permissions.
Memberships have no standalone admin. Viewers/editors cannot mutate cases or
memberships even with global permissions.

### Read-only inspection

Staff with the model's view or change permission can search and inspect records
in their accessible cases. Invitation and Instagram approval inspection also
requires case ownership. Read-only records have no add, save, or delete actions.
Use the case workflow for approvals, retries, edits, and current evidence.

| Admin section | Records available for inspection |
| --- | --- |
| Cases | Activity revisions and owner-only invitation status |
| Core | Analysis batches, runs, results, findings, sources, reviews, request accounting, artifacts, and extraction metadata. Evidence chat, Important items, retained selections, and case search index metadata. |
| Geo | Import batches, points of interest, and saved trip processing and provenance |
| Phone | Communication context, conversation grouping, requests, and saved sessions. Call attachments appear on the associated call. |
| Social media | Reported accounts, approved Instagram targets, monitors, checks, captures, and comparisons |

These pages show status, identifiers, dates, and provenance where applicable.
Open import rows, trip observations, and session messages in the linked case
workflow. Read file contents, transcripts, and findings in their case readers.
Invitation digests, private storage keys, Instagram sessions, model payloads, and
search vectors are omitted from inspection. Personal Instagram connections are
managed from account settings. Conversation pair revisions are internal processing
state.

Run the [worker diagnostics](background-tasks.md#failure-inspection-and-recovery)
or [file cleanup recovery](evidence.md#private-storage-and-deletion) for operational issues.
Queue records, pending file cleanup, and temporary evidence deletion reviews
have no admin lists. Read-only records can still be removed by an authorized
parent deletion when the existing lifecycle rules call for it.

## Scope and migration

Apply the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
with the [migration workflow](development.md#run-python-on-the-host). The initial
case/membership/invitation schema depends on the configured user model.

`cases.permissions` scopes product requests independently of staff/model
permissions. Requests and [background analysis](analysis.md) must check current
access. Public APIs and ownership transfer remain planned.

The implementation follows Django's [intermediary model pattern](https://docs.djangoproject.com/en/6.1/topics/db/models/#extra-fields-on-many-to-many-relationships)
and [admin permission hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission).
