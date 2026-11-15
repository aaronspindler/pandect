# Cases and collaboration

`cases.Case` is the investigation and access boundary. Every evidence record
requires a case; see [evidence storage](evidence.md). `cases` owns ownership,
collaboration, invitations, settings, authorization, and case navigation. `core`
owns Evidence, Processing, and the Case Tools workspace; `geo` owns maps and POIs.

## Investigator workflow

Sign in with a verified account and choose **All cases → Create case**. Enter a
name; the signed-in user becomes owner. Opening, creating, or joining a case
opens **Case Tools** after checking access. Saved case URLs open the same home.
The shared navigation links directly to **Case Tools**, **Evidence**, and
**Processing**. Case Tools provides recent evidence and source-linked findings,
plus Map, Chronology, Connections, People, Vehicles, Selections, and reports.
A **Case details** disclosure retains ownership, timestamps, your role, and any
saved read-only description; it also contains owner-only Settings.
All cases searches accessible names/descriptions and filters owned/shared cases.

Viewers can inspect case material and download originals. Editors can also add or
edit investigative records and archive/restore evidence. Owners manage access and
can delete people, vehicles, or the whole case, subject to retained-evidence
protections. Product users need no staff status or Django model permissions.
See [maps](maps.md) for the Map workflow, coverage, and privacy.

Owners rename cases and manage collaborators in Settings. Stale edits preserve the
draft beside newer saved values and require **Save reviewed changes**. Under
**Delete this case**, **Delete case** opens a named confirmation including affected
records and archived evidence. There is no in-app undo. User accounts remain;
[operations](operations.md) and [evidence](evidence.md) describe storage/backup limits.

## Account settings

Choose the Account icon in the shared navigation to manage email addresses,
passwords, or your timezone.
Verified secondary addresses offer **Make primary**. Removing secondary addresses
requires confirmation; unverified addresses offer **Resend verification**.
Address changes follow django-allauth's verification and authorization rules.

Times and date/time fields use the device's IANA timezone by default. JavaScript
detects it and stores a `pandect_timezone` browser cookie; **Account → Timezone**
can instead save an explicit timezone on your account, including when JavaScript
is disabled. An account preference applies across devices. **Automatic** resumes
device detection. Until a timezone is available, pages use UTC. Timezone labels
follow the date's daylight-saving offset; ambiguous or nonexistent local form
times are rejected for review.

This changes presentation and form interpretation, not stored instants, retained
originals, or source-export timezone choices. Downloads retain their recorded
timestamps, and analysis statistics explicitly grouped by UTC hour/weekday keep
that basis.

## Invitations

In **Settings → Invite collaborator**, enter an email and choose Viewer (default)
or Editor. Recipients need an account and must verify the invited email.
Registration follows `PANDECT_ALLOW_SIGNUPS` (default `true`); when closed, new
recipients need an operator-provisioned account. Invitations do not bypass this.
Acceptance requires POST and resumes after sign-in/email verification; opening a
link alone grants no access.

Links expire after `PANDECT_INVITATION_DAYS` (default seven days). Owners can
resend, revoke, or change a pending role; resend/role changes invalidate old tokens.
Mail failure leaves an actionable pending invitation; check SMTP before resending.
Pending/failed invitations remain visible, with accepted/revoked records under
**Invitation history**. `PANDECT_PUBLIC_ORIGIN` supplies email link origins.

Records retain token digests. Authentication sessions may retain continuation
URLs: treat sessions as secrets and never log them. Acceptance rechecks expiry,
revocation, verified recipient, and case state under a case lock. Replay cannot
duplicate memberships or restore revoked access; an existing member's role is
preserved. Invitations to owners/current members are rejected. Membership changes
never transfer ownership or create another owner.

## Data model

A case has one required owner. Product creation requires a name; trusted ORM/admin
callers that omit it receive `Case #<UUID>`. Saved descriptions remain read-only.
Status and ownership transfer are not implemented. The [models](../cases/models.py)
own field definitions and constraints.

`Case.additional_users` uses `CaseMembership`, with a required case, user, and
`viewer` (default) or `editor` role. PostgreSQL enforces one membership per
case/user and valid roles. Model validation/admin reject the owner
as a member; this is not a cross-table database constraint. ORM callers must
invoke `full_clean()` before saving memberships.

Case deletion cascades to memberships, people, vehicles, and evidence, with file
cleanup after commit. Deleting a collaborator removes memberships and retains
cases; protected attribution (such as evidence uploads or invitations) may block
account deletion. The owner's foreign key uses `PROTECT`, including through user
admin/queryset deletion.

## Django admin

Open `/admin/` and choose **Cases**. All scoped administration requires active
staff status, global model permissions, and current case access. Superusers have
no case-access bypass. Case membership alone grants no global model permissions.

For case-scoped domain records, inspection requires `view_<model>` or
`change_<model>` and any current case role. Creation requires `add_<model>` and
an owner/editor-access case; editing requires `change_<model>` and owner/editor
access. Permission names use the owning app, such as `people.add_person` or
`document.view_documentevidence`. Individual deletion follows the domain guide:
[people](people.md#django-admin), [vehicles](vehicles.md#django-admin-and-authorization),
or [evidence](evidence.md#permanent-deletion).

Lists, searches, direct/history/deletion URLs, submitted case choices, and inline
IDs are scoped server-side. Revocation or downgrade affects subsequent requests
in existing sessions. Existing domain records cannot be reassigned to another
case, and bulk admin actions are disabled. These shared gates are implemented in
[`CaseScopedAdmin`](../cases/admin_access.py); case and membership administration
adds the owner-only rules below.

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| Create | The signed-in user becomes owner | `cases.add_case` |
| List, inspect metadata/history | Owner, viewer, or editor | `cases.view_case` or `cases.change_case` |
| Change case | Owner | `cases.change_case` |
| Add collaborator | Owner of saved case | `cases.change_case` and `cases.add_casemembership` |
| Change collaborator/role | Owner of saved case | `cases.change_case` and `cases.change_casemembership` |
| Remove collaborator | Owner of saved case | `cases.change_case` and `cases.delete_casemembership` |
| Delete case | Owner | `cases.delete_case` and contained-model deletion permissions |

Owners need `cases.view_casemembership` or `cases.change_casemembership` to inspect
membership inlines. Assign permissions through user/group admin. Case deletion
also needs `people.delete_person`, `vehicle.delete_vehicle`, and other permissions
listed on the confirmation when those records are present.

Choose **Add case → Save and continue editing**. Ownership is assigned server-side;
owner/creation time then become read-only. The owner-only membership inline adds
existing users, changes roles, or removes members, respecting model permissions.
Memberships have no standalone admin.

Viewers/editors cannot mutate cases or memberships even with global permissions.

## Scope and migration

Apply the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
using the [migration workflow](development.md#run-python-on-the-host). The initial
case/membership/invitation schema depends on the configured user model.

`cases.permissions` scopes product requests independently of staff/model
permissions. Requests and [background analysis](analysis.md) must check current
access. Public APIs and ownership transfer remain planned.

The implementation follows Django's [intermediary model pattern](https://docs.djangoproject.com/en/6.1/topics/db/models/#extra-fields-on-many-to-many-relationships)
and [admin permission hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission).
