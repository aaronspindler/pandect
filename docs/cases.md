# Cases and collaboration

Every evidence record belongs to a case. Cases control investigator access,
collaboration, invitations, and settings.

## Investigator workflow

With a verified account, choose **All cases → Create case** and enter a name.
You become the owner. Opening, creating or joining a case leads to **Case Tools**,
with links to case workspaces, reports and **Latest records**. Owners and
editors can **Add evidence** and set up methods directly. Owners can open
**Case settings** from the header. **Case details** shows the owner, your role,
case dates and any saved description.

**Search this case** uses the private Cloudflare AI Search index when enabled.
Results link back to current case sources. Browsing and structured filters
remain available with external AI disabled. See the [investigator
interface](interface-design.md) for navigation and shortcuts.

Viewers inspect material and download originals. Editors also add/edit records
and archive/restore evidence. Owners manage access and delete people, vehicles,
or the case, subject to retained-evidence protections. Product users need no staff
status or model permissions. See [maps](maps.md) for Map coverage and privacy.

Private pages are served without caching. Back and Forward recheck access
before displaying a private page. When the workspace detects a session or role
change, it clears the previous contents and offers **All cases**.

### Important items

Owners and editors can **Mark important** beside Findings on active evidence,
on a finding, or on a whole Evidence chat reply. **Case Tools → Important**
lists the marks, newest first. Open the exact source or **Notes**, then use
**Back to Important** to return to its page. Sighting proposals retain their
label and show their current review outcome.

Every case member can read dated notes and see their authors. Owners and editors
can add entries while the item is marked. **Remove from Important** (under
**More** on evidence) hides the item but keeps notes at the source. Mark it
again to restore the list entry. Evidence marks and notes round trips retain
the collection's search, filters and page. Evidence chat excludes these dated
Important notes from retrieval and model prompts.

Archiving retains marks and notes for restoration, while Important hides the
archived evidence and saved findings or replies with unavailable sources.
Superseding a finding keeps its mark on the saved version. Permanent evidence
deletion removes affected marks and notes in the reviewed inventory. Whole-case
deletion removes all case marks and notes.

## Case settings

Settings leads with collaborators and invitations. Owners open **Rename case**
to edit the name. Stale edits reopen the form and show the draft beside newer
values for **Save reviewed changes**.
**Delete this case → Delete case** opens a named confirmation listing affected
records, including archived evidence. There is no in-app undo; user accounts
remain. See [operations](operations.md) and [evidence](evidence.md) for storage
and backup limits.

## External AI and hosted search

Cases start with external AI disabled. A case owner can **Enable external AI**
after reviewing the Cloudflare account and namespace, Agent and model
destinations, and transfer scope. Enabling creates one private AI Search
instance and queues indexing. The scope includes active evidence and derived
passages, reports, case records, social accounts, places, and legacy notes on
person, vehicle and POI records. These indexed record fields can also be
retrieved by Case Chat. See [indexed material](cloudflare-search.md).

**Models by task** assigns models and supported reasoning effort. **Approved
destinations** shows pending approvals and revocation controls. Text, vision,
transcription, and geographic lookup have distinct approvals. See
[configuration and transfer scope](analysis.md#external-ai-configuration).

**Disable external AI** stops new hosted work, invalidates pending AI work,
and queues deletion of the private search instance. Authorized users retain
saved results, chats and source citations, and can still browse, import and run
deterministic checks. Re-enabling requires current owner approval.

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
