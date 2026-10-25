# Cases and collaboration

`cases.Case` is the concrete root investigation record and access boundary. Future
investigation data must reference this model. Every implemented evidence model
has one required case; see [evidence storage](evidence.md). There is no separate
investigation model or unscoped collection of evidence.

## Data model

A case has Django's default `BigAutoField` primary key, a required `owner` pointing
to `settings.AUTH_USER_MODEL`, and an automatically recorded `created_at` timestamp
that remains unchanged on later saves. It is displayed as `Case #<id>`; title and
status fields are not implemented.

`Case.additional_users` is an optional many-to-many relationship through
`CaseMembership`. Each membership has a required case, user, and role: `viewer`
(the default) or `editor`. PostgreSQL constraints enforce one membership per
case/user pair and reject invalid roles. A collaborator may belong to multiple
cases. Membership changes never modify the case's owner.

Membership model validation (`full_clean()`) and admin forms reject the owner as
an additional user. As with ordinary Django models, direct ORM writes do not
automatically call `full_clean()`; code creating memberships must validate them.
The owner exclusion is model validation, not a cross-table database constraint.

Deleting a case cascades to its memberships, people, and evidence. Evidence files
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

`cases/0001_initial` is an additive schema migration with a swappable dependency
on the configured user model. Run the usual `python manage.py migrate` command
with the documented environment, or start the updated Compose image. No legacy
data import, runtime upgrade, or dependency change is required. Back up an existing
database before applying migrations as described in [operations](operations.md).

This implements the root records and admin workflow. Invitations, public APIs,
investigator-facing screens, ownership transfer, evidence upload/download
workflows, and background/AI processing remain planned. Viewer/editor roles are
used for case material permissions: editors can create and edit people, while
viewers can inspect them. Both roles have read-only case metadata access in admin.
Ordinary non-staff collaborators will need the future product interface to access
cases.

The implementation follows Django's [intermediary model pattern](https://docs.djangoproject.com/en/6.1/topics/db/models/#extra-fields-on-many-to-many-relationships)
and [admin permission hooks](https://docs.djangoproject.com/en/6.1/ref/contrib/admin/#django.contrib.admin.ModelAdmin.has_view_permission).
