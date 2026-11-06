# Cases and collaboration

`cases.Case` is the concrete root investigation record and access boundary. Future
investigation data must reference this model. Every implemented evidence model
has one required case; see [evidence storage](evidence.md). There is no separate
investigation model or unscoped collection of evidence.

## Investigator workflow

Sign in with a verified account and open **All cases**. Choose **Create case**,
enter a name and optional description, then add evidence, people, or vehicles from the case
navigation. The signed-in user becomes the owner. All cases supports scoped name/
description search and owned/shared filtering; its results never include unrelated
cases, even for superusers. The overview prioritizes recent evidence, analysis
entry points, and compact people and vehicle count links; **Case details** reveals
the owner, access role, and creation/update times.

Owners can edit the name/description under **Settings**, invite collaborators,
change their Viewer/Editor roles, and remove access. Viewers can inspect people, vehicles,
and active/archived evidence and download originals. Editors can additionally
add/edit people, vehicles, and evidence and archive/restore evidence; only the owner can
change case metadata, manage access, delete people or vehicles, or delete the case.
Ordinary product users do not need staff status or Django model permissions.
Choose **Map** to explore recorded location coordinates against OpenStreetMap,
with source links and a location list. See [maps](maps.md) for coverage, filters,
background map configuration and evidence privacy.

Case edits detect stale updates. A conflict shows the latest saved details beside
the retained draft and offers **Save reviewed changes**; it does not silently
replace another investigator's changes. In Settings, expand **Delete this case**
to find **Delete case**, which opens a separate named
confirmation showing affected records, including archived evidence. There is no
in-app undo. User accounts remain; storage cleanup/backups have the limits
described in [operations](operations.md) and [evidence](evidence.md).

## Account settings

Choose your account in the sidebar to manage email addresses or your password.
A verified primary email is displayed without redundant actions. Verified secondary
addresses offer **Make primary**. Secondary addresses have a named **Remove address** confirmation;
unverified addresses also offer **Resend verification**. Adding or changing
addresses continues through django-allauth's verification and authorization rules.
Operator user administration keeps account identity and access flags visible;
**Advanced permissions** and **Important dates** expand when needed.

## Invitations

Under **Settings → Invite collaborator**, enter an email and choose Viewer
(default) or Editor. The recipient may register or use an existing account, but
must verify the invited email address before accepting. Invitation acceptance is
an explicit POST; merely opening a link does not grant access. Pending links are
resumed after sign-in/email verification.

Invitations expire after `PANDECT_INVITATION_DAYS` (default seven days). Owners can
resend, revoke, or change a pending role. Resending/changing the role invalidates
the old token. Email delivery failure leaves an actionable pending invitation;
check SMTP configuration before resending. Settings keeps pending or failed invitations
visible; accepted and revoked invitations are available under **Invitation history**. `PANDECT_PUBLIC_ORIGIN` supplies the
email link's deployment origin.

Invitation records store a token digest rather than the raw link token. Private
authentication sessions can retain the local continuation URL while sign-in or
email verification completes; treat session contents as secrets and never log them.
Acceptance rechecks
expiry, revocation, recipient verification, and current case state under a case
lock. Replay never adds duplicate memberships or restores revoked access.
Acceptance preserves an already-existing member's role. Invitations to owners or
current collaborators are rejected; use member controls instead. Ownership is
never an invitation role or transferred by membership changes.

## Data model

A case has an automatically generated UUIDv4 primary key, a required `owner` pointing
to `settings.AUTH_USER_MODEL`, and an automatically recorded `created_at` timestamp
that remains unchanged on later saves. It also has a required `name` (up to 255
characters), optional `description`, and automatic `updated_at`. Product creation
requires an explicit name; trusted ORM/admin callers that omit it receive the
fallback `Case #<UUID>`. Case names do not establish a separate ownership boundary.
There is no case status or ownership-transfer feature.

`Case.additional_users` is an optional many-to-many relationship through
`CaseMembership`. Each membership has its own UUIDv4 primary key and a required
case, user, and role: `viewer`
(the default) or `editor`. PostgreSQL constraints enforce one membership per
case/user pair and reject invalid roles. A collaborator may belong to multiple
cases. Membership changes never modify the case's owner.

Membership model validation (`full_clean()`) and admin forms reject the owner as
an additional user. As with ordinary Django models, direct ORM writes do not
automatically call `full_clean()`; code creating memberships must validate them.
The owner exclusion is model validation, not a cross-table database constraint.

Deleting a case cascades to its memberships, people, vehicles, and evidence. Evidence files
are removed after the deletion commits. Deleting a collaborating user removes
their memberships and preserves the case, unless retained evidence protects that
user as its uploader. The owner's foreign key uses `PROTECT`:
an owner cannot be deleted while they own a case, including through user admin or
queryset deletion. Ownership transfer is not implemented in admin.

## Django admin

Open `/admin/` and choose **Cases**. Django's active staff requirement and global
model permissions apply in addition to case access; a membership alone does not
grant access to admin. Superusers receive the usual global model permissions but
must still own or share a case to inspect it.

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| Create a case | The signed-in user becomes its owner | `cases.add_case` |
| List or inspect metadata and history | Owner, viewer, or editor | `cases.view_case` or `cases.change_case` |
| Submit case changes | Owner | `cases.change_case` |
| Add collaborators | Owner of a saved case | `cases.change_case` and `cases.add_casemembership` |
| Change a collaborator or role | Owner of a saved case | `cases.change_case` and `cases.change_casemembership` |
| Remove a collaborator | Owner of a saved case | `cases.change_case` and `cases.delete_casemembership` |
| Delete a case and its memberships | Owner | `cases.delete_case` |

Deleting a case containing [people](people.md) also requires
`people.delete_person` in Django admin, because those records are deleted too.
Cases containing [vehicles](vehicles.md) also require `vehicles.delete_vehicle`.

Owners need `cases.view_casemembership` or `cases.change_casemembership` to inspect
existing memberships in the inline. Assign the appropriate case and membership
permissions to staff users or Django groups through user/group admin.

To create a case, choose **Add case**, then **Save and continue editing**. Ownership
is assigned on the server; a submitted owner field cannot choose a different user.
After saving, owner and creation time are read-only. Use the membership inline to
select existing users and viewer/editor roles, or mark rows for removal, then save.
The inline is available only on cases the current user owns and respects each
membership model permission. Memberships have no standalone admin, and case bulk
actions are disabled.

Case lists, metadata, history, and deletion URLs use the same scoped queryset.
Submitted membership IDs and inline case fields are restricted to their parent
case. Viewers and editors can inspect case metadata and history; they cannot
submit case changes, manage memberships, or delete cases, even if they hold the
global change/delete permissions. Removing a membership removes access on the
next request, including requests in an existing session.

## Scope and migration

`cases/0001_initial` creates the UUID schema with a swappable dependency
on the configured user model. Run the usual `python manage.py migrate` command
with the documented environment, or start the updated Compose image. The
[pre-release UUID baseline](architecture.md#primary-keys-and-the-initial-schema)
requires a fresh database if the earlier integer initial migrations were applied.
No legacy data import, runtime upgrade, or dependency change is included.

`cases/0002_case_interfaces` adds case metadata and `CaseInvitation` without
rewriting initial migrations. It backfills existing names as `Case #<UUID>` and
initializes update times from original creation times. Memberships, ownership,
evidence, and identifiers remain intact. Back up before applying upgrades.

The product routes implement the workflows above. `cases.permissions` scopes
product requests independently of staff/model permissions; the admin retains its
additional operator gates. Public APIs, ownership transfer, and background/AI
processing remain planned. Case material requires current access on each request,
including after a membership is removed or downgraded.

The implementation follows Django's [intermediary model pattern](https://docs.djangoproject.com/en/6.1/topics/db/models/#extra-fields-on-many-to-many-relationships)
and [admin permission hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission).
