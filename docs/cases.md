# Cases and collaboration

`cases.Case` is the investigation/access boundary: every evidence record requires
a case. `cases` owns collaboration, invitations, settings, authorization, and
navigation; `core` owns Evidence, Processing, and Case Tools; `geo` owns maps/POIs.

## Investigator workflow

With a verified account, choose **All cases → Create case** and enter a name.
The creator becomes owner. Opening, creating, or joining a case checks access
and opens **Case Tools**, as do saved case URLs. Shared navigation links to
**Case Tools**, **Evidence**, and **Processing**. Case Tools groups evidence views,
organizing tools, and reports in one directory, with recent evidence and
source-linked findings below. Its header also links to Evidence and Processing
and offers **Add evidence** to owners/editors. **Case details** holds ownership,
timestamps, your role, saved read-only descriptions, and owner-only Settings.
All cases searches accessible names/descriptions and filters owned/shared cases.

Viewers inspect material and download originals. Editors also add/edit records
and archive/restore evidence. Owners manage access and delete people, vehicles,
or the case, subject to retained-evidence protections. Product users need no staff
status or model permissions. See [maps](maps.md) for Map coverage and privacy.

Owners rename cases and manage collaborators in Settings. Stale edits show the
preserved draft beside newer values and require **Save reviewed changes**.
**Delete this case → Delete case** opens a named confirmation listing affected
records, including archived evidence. There is no in-app undo; user accounts
remain. See [operations](operations.md) and [evidence](evidence.md) for storage
and backup limits.

## Account settings

The Account icon opens email, password, and timezone settings. Verified secondary
addresses offer **Make primary**; unverified addresses offer **Resend verification**.
Removing secondary addresses requires confirmation. Address changes follow
allauth's verification/authorization rules.

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

Links expire after seven days. Owners can
resend, revoke, or change pending roles; resend/role changes invalidate old tokens.
Mail failure retains an actionable pending invitation: check SMTP before resending.
Pending/failed invitations remain visible; accepted/revoked records appear under
**Invitation history**. `PANDECT_PUBLIC_ORIGIN` supplies email link origins.

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
| Change case | Owner | `cases.change_case` |
| Add collaborator | Owner of saved case | `cases.change_case` and `cases.add_casemembership` |
| Change collaborator/role | Owner of saved case | `cases.change_case` and `cases.change_casemembership` |
| Remove collaborator | Owner of saved case | `cases.change_case` and `cases.delete_casemembership` |
| Delete case | Owner | `cases.delete_case` and contained-model deletion permissions |

Owners need `cases.view_casemembership` or `cases.change_casemembership` to inspect
membership inlines. Assign permissions through user/group admin. Case deletion
also requires permissions for contained records, such as `people.delete_person`
and `vehicle.delete_vehicle`, as listed on confirmation.

Choose **Add case → Save and continue editing**. The server assigns ownership;
owner/creation time then become read-only. The owner-only inline adds existing
users, changes roles, or removes members subject to model permissions.
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
