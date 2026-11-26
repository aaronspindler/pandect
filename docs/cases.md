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
their sources. With external AI enabled, Cloudflare AI Search finds matches in
the private case index. You can browse and filter retained records while external
AI is disabled.
See the [investigator interface](interface-design.md) for shortcuts and filters.

Viewers inspect material and download originals. Editors also add/edit records
and archive/restore evidence. Owners manage access and delete people, vehicles,
or the case, subject to retained-evidence protections. Product users need no staff
status or model permissions. See [maps](maps.md) for Map coverage and privacy.

When the workspace detects a session or role change, it clears the previous
case contents and offers a link to **All cases**. Reopening a private page with
Back or Forward checks the current session and access before displaying it.

Private pages are served without caching. Going back to a private page in the
browser checks access again. If an open workspace detects a sign-in change or a
changed case role, it discards the previous page and offers **All cases** to
continue under the current access rules.

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

Settings leads with collaborators and invitations. Owners open **Rename case**
to edit the name. Stale edits reopen the form and show the draft beside newer
values for **Save reviewed changes**.
**Delete this case → Delete case** opens a named confirmation listing affected
records, including archived evidence. There is no in-app undo; user accounts
remain. See [operations](operations.md) and [evidence](evidence.md) for storage
and backup limits.

## External AI and hosted search

Case owners can **Enable external AI** in Case settings. Every case starts with external AI disabled. Review the Cloudflare account, production namespace, investigation Agent URL, and model destinations before approving. Enabling creates a private AI Search instance for this case and queues indexing of active evidence passages and findings. Search queries, investigation questions, and relevant excerpts go through the approved Cloudflare services. Case Search also indexes source descriptions, reports, people and contact details, reported social accounts, vehicles, places of interest and investigator notes.

Use **Models by task** to assign a model and supported reasoning effort. Each provider and material type has a separate approval. Text approvals cover case passages, questions, and source references. Vision approvals cover prepared images, plate crops and scanned PDF pages. Transcription approvals cover recording chunks. Inspect **Approved destinations** to review pending providers or revoke one. Geographic lookup uses its own approval.

**Disable external AI** stops new hosted work, invalidates pending AI work and queues deletion of the private search instance. Saved results, chats and source citations remain available to authorized case members. Native document extraction, metadata, imports and deterministic analysis remain available. Re-enabling requires current owner approval. Operators configure endpoints and credentials for both the web application and the worker. See [AI Gateway settings](analysis.md#external-ai-configuration).

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
post-commit file cleanup. A deletion stops if it would affect a related record or
file linked outside the case. Deleting a collaborator removes memberships and retains
cases; protected attribution, including uploads/invitations, may block account
deletion. The owner foreign key uses `PROTECT`, including admin/queryset deletion.

## Django admin

The operator console is titled **Pandect administration**.
Hosted sites can also configure a [request activity view](request-activity.md)
with separate permissions for site operators.

Open `/admin/` → **Cases**. Scoped administration requires active staff status,
global model permissions, and current case access. Superusers have no access
bypass; membership grants no global permissions.

Anonymous admin sign-in requests redirect to the normal allauth sign-in flow,
including email verification and shared authentication rate limits.

Domain inspection requires `view_<model>` or `change_<model>` and any case role.
Creation requires `add_<model>` and owner/editor access; editing requires
`change_<model>` and owner/editor access. Permission names use the owning app,
such as `people.add_person` or `document.view_documentevidence`. See the deletion
rules for [people](people.md#django-admin), [vehicles](vehicles.md#django-admin-and-authorization),
[evidence](evidence.md#permanent-deletion), and [saved chats](#delete-an-evidence-chat).

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
Account deletion summarizes the selected accounts. Retained case records can
block deletion. The confirmation includes details of those records only when
the operator currently has access to their cases.

Choose **Add case → Save and continue editing**. The server assigns ownership.
Existing case details are read-only in admin. To rename a case, use
[Case settings](#case-settings). The owner-only inline adds existing users,
changes roles, or removes members subject to model permissions.
Memberships have no standalone admin. Viewers/editors cannot mutate cases or
memberships even with global permissions.

### Delete an Evidence chat

Staff case owners with `core.delete_evidencechat` and the chat's view or change
permission can open **Core → Evidence chats**, select a chat, and choose
**Delete**. Review the chat's case and deletion counts by record type, check the
confirmation box, and choose **Permanently delete chat**. Deletion permanently
removes the chat's replies, saved source records and citations, request
accounting, and any Important marks and notes attached to those replies. Wait
for queued or running replies and approved extractions to finish before
deleting. Original evidence and saved extraction results remain in the case.

### Read-only inspection

Staff with the model's view or change permission can search and inspect records
in their accessible cases. Invitation and Instagram approval inspection also
requires case ownership. Saved Evidence chats have an
[owner-only delete action](#delete-an-evidence-chat) on the inspection page.
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
