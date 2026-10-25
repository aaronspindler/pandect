# People

`people.Person` is an investigator-maintained record belonging to exactly one
`cases.Case`. A case can contain many people through `case.people.all()`. People
are independent of login accounts: a matching name, email, or phone number is not
proof of identity, and matching records are not automatically linked or merged.
These records capture investigative work, not verified evidence or model output.

## Fields and validation

| Field | Behavior |
| --- | --- |
| `id` | Automatically generated, non-editable UUIDv4 primary key. |
| `case` | Required case foreign key. Deleting the case deletes its people. |
| `first_name`, `last_name` | Optional text, up to 150 characters each. |
| `phone_number` | Optional text, up to 64 characters; preserves international formatting and extensions. |
| `email` | Optional Django-validated email address, up to 254 characters. |
| `address` | Optional multiline address, without country-specific structure. |
| `notes` | Optional multiline investigator notes; preserve uncertainty and conflicting observations. |
| `created_at` | Set automatically on creation. |
| `updated_at` | Set automatically on model saves. Queryset updates do not update it automatically. |

Names may be unknown. At least one of first name, last name, phone number, or email
must contain a non-whitespace character. A PostgreSQL check constraint enforces
this, including for direct and bulk database writes. Address and notes alone do
not meet this requirement. Missing text values use empty strings, not `NULL`.

As with ordinary Django models, call `full_clean()` before saving through the ORM
to validate email and field lengths; `save()` does not automatically run model
validation. Admin forms validate these fields automatically. The phone field does
not normalize or validate telephone numbering plans. There is one phone number,
email, and address per record. Names and contact details are not unique, either
within a case or across cases.

A person's display name uses their first and last name, falling back to email
and then phone number. Creation/update timestamps and Django admin history are
not a versioned evidence or identity-assertion history.

## Django admin

After creating a case, open `/admin/` and choose **People → Add person**. Choose
an owned case or one where you are an editor, enter at least one identifying
field, and save. Existing records have read-only case and timestamp fields. Case
reassignment is not supported in admin, including to another case you own.

Active staff status and ordinary Django model permissions are required in addition
to current case access:

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| List, search, inspect a person or history | Owner, viewer, or editor | `people.view_person` or `people.change_person` |
| Create a person | Owner or editor of the selected case | `people.add_person` |
| Edit a person | Owner or editor | `people.change_person` |
| Delete a person | Owner only | `people.delete_person` |

Assign permissions through Django's user/group administration. Case model
permissions alone do not grant person permissions. An editor can edit people
without being allowed to change case metadata or manage collaborators. Users
without an editable case cannot open the person creation form. Ordinary non-staff
collaborators require the future investigator-facing interface.

Lists and searches include only accessible cases. Direct object, history, and
deletion URLs use the same case boundary. Submitted case choices are restricted
on the server, bulk actions are disabled, and removing or downgrading a membership
affects subsequent requests in existing sessions. Superusers have no case-access
bypass.

Deleting a case cascades to its people. Django admin also requires
`people.delete_person` when a case contains people, in addition to
`cases.delete_case`; the confirmation screen lists missing permissions. Deleting
an individual person leaves its case and other people intact.

## Migration and scope

`people/0001_initial` creates the UUID person table with a dependency on
`cases/0001_initial`. Apply it with the [documented migration workflow](development.md),
or start the updated Compose image. See the
[pre-release UUID baseline](architecture.md#primary-keys-and-the-initial-schema)
for databases that applied the earlier integer migrations. No data conversion,
dependency changes, or runtime upgrades are included.

Authorization is enforced by the current admin entry points, not automatically
by ORM queries. Future views, exports, background jobs, and integrations must
check current case permissions themselves. Public APIs, investigator-facing
screens, multiple contact records, and evidence/identity linkage remain planned.
