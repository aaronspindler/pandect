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
**More** menu or on an individual finding. **Case Tools → Important** lists
the case's marked items, newest mark first. Open the title to revisit the exact
record or finding. **Back to Important** returns to its list page.
Finding summaries keep their original labels and show the current review outcome
for sighting proposals.

Open **Notes** to read dated entries and see who added each one. Owners and
editors can add entries while the item is marked. Viewers can read the list
and notes. Marking evidence and returning to it from **Notes** retain the active
Evidence search, filters, and page. **Remove from Important** hides the item
from the list. Its notes remain accessible from the source, and marking it
again restores the list entry.
Notes are investigator annotations. They are separate from evidence, findings,
and review decisions.

Archiving evidence or replacing a finding in current results keeps the mark on
the saved item. If a finding's sources become unavailable, the finding and
its notes are withheld. Permanent evidence deletion removes affected marks and
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

## AI backend

The case owner chooses **Local AI** (the default) or **External AI** under
**Case settings → AI processing**. The selection applies to new automatic checks,
evidence reprocessing, and on-demand AI checks without a dedicated service.
Each check uses the selected backend when available. For External AI, approve
the destination before selecting it. A missing service or approval leaves the
AI check unavailable. Built-in checks and geographic services have their own
requirements.

Changing the selection applies to new requests. Queued requests and saved
results keep their original service. Revoke external approval to stop subsequent
requests to approved external destinations, including requests from queued work.

## External analysis approval

The case owner can approve or revoke the configured external services under
**Case settings → External analysis**. The review lists each destination and the
data it receives. Analysis can include newly added evidence, selected material,
and relevant active case history. Approval applies to this case and the displayed
destinations. Changes to a destination require approval in Case Settings before
Pandect sends evidence there. Revocation stops subsequent requests. Provider
retention still applies to requests already sent.

New cases start without external approval. Owners and editors can start
**Reprocess evidence** for retained material.

## Hosted meaning search

Under **Case settings → Meaning search**, the verified owner reviews the Cloudflare
account and gateway, OpenAI key alias and embedding model. **Approve and start
indexing** queues existing eligible passages and covers future additions. This
includes archived material and the text of queries that case members explicitly
submit with **Search by meaning**. Keyword searches stay in Pandect.

**Processing → Meaning-based search** shows indexing progress and how to resume stopped
work. Changing the embedding destination requires a fresh approval. **Revoke
meaning search approval** stops subsequent transfers, including queued work.
Revocation preserves retained evidence. Search approval is independent of the
case’s AI backend and external analysis approval.

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

## Scope and migration

Apply the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
with the [migration workflow](development.md#run-python-on-the-host). The initial
case/membership/invitation schema depends on the configured user model.

`cases.permissions` scopes product requests independently of staff/model
permissions. Requests and [background analysis](analysis.md) must check current
access. Public APIs and ownership transfer remain planned.

The implementation follows Django's [intermediary model pattern](https://docs.djangoproject.com/en/6.1/topics/db/models/#extra-fields-on-many-to-many-relationships)
and [admin permission hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission).
