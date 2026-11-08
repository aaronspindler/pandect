# People

`people.Person` is an investigator-maintained record belonging to exactly one
`cases.Case`. A case can contain many people through `case.people.all()`. People
are independent of login accounts: a matching name, email, or phone number is not
proof of identity, and matching records are not automatically linked or merged.
These records capture investigative work, not verified evidence or model output.

## Investigator workflow

Open a case and choose **Case tools → People**. Owners and editors can choose
**Add person**, enter known name or contact details, and save. At least
one name, phone number, or email is required. Viewers can inspect records without
mutation controls. The searchable list and all detail/edit/delete routes remain
scoped to the current case.

Choose **Edit person** to correct contact and name details. A stale form
shows newer saved values alongside the preserved draft and requires explicit
**Save reviewed changes**. A removed/deleted record is not recreated by a stale
form. Case reassignment, identity verification, and automatic merging are not
supported.

The detail page keeps contact details, social accounts, and POIs visible.
**Record details** reveals saved read-only notes, name components, timestamps, and the owner's
**Delete person** action. Empty notes and repeated name fields do not occupy the
main reading area. Contact details show only populated values; when none are
recorded, a single message replaces the empty field labels. A concise caption
keeps the record's unverified status visible beside its title.

Only the owner can delete an individual person, after a separate named-target
confirmation. Deleting an unlinked person retains evidence, login accounts, and
other people with matching contacts. A person referenced by retained
person-location evidence or a sender/recipient link in communication evidence
cannot be deleted individually, even if the evidence is
archived. Whole-case deletion still removes both together; see [location
observations](evidence.md#location-observations). Ordinary investigators do not
need Django admin access.

### Social accounts

Open a person and choose **Add social account** to record a platform and a
reported username. Enter the platform by name and preserve the username's
spelling and punctuation. An optional **Source / citation** records where the
association was reported. A username is an investigator-recorded association
whose ownership remains unverified.

A person can have several accounts, including multiple usernames on the same
platform. An exact platform and username pair can appear once per person.
The People list searches platform names and usernames within the current case.
Viewers can inspect and search accounts. Owners and editors can also edit or
confirm removal. Concurrent edits show the latest saved values alongside the
preserved draft. If the details change before removal, review them and confirm
again. Accounts are managed in the investigator interface and are removed with
their person record or case.

`socialmedia.SocialAccount` owns the account table. Its forms, views, templates,
and tests live in `socialmedia`; the person detail page continues to show these
accounts through the person relationship.

### Points of interest

On a person detail page, choose **Add POI** to record a **Home**, **Work**, or
custom-labelled place. The person is preselected; latitude/longitude are
required, while address and source/citation are optional. Existing POI notes
remain readable on the POI detail page.
The case map also offers **Add POI** and pointer-based position selection.
POIs remain separate from the person's contact address and retained location
evidence. A label is an investigator assertion, not verified residence,
employment, or presence.

Owners and editors can inspect, edit, and confirm deletion of POIs. Viewers have
read-only access. Edits detect newer saved changes and preserve the proposed
values for explicit review. Each person has a paginated POI list (25 per page);
the case map plots its current POI page with distinct diamond markers. POIs
retain creator and creation/update times, but no versioned history. On a POI
detail page, **Record details** reveals those timestamps and the editor's
**Delete POI** action. Deleting
a person also deletes their POIs; the confirmation explains this consequence.
They do not add another restriction on person deletion.

`geo.PointOfInterest` owns points of interest, with forms, views, templates, and
tests in `geo`. People retain their own contact fields and management workflows. See [the map workflow](maps.md#person-points-of-interest)
for coordinate bounds, search, permissions, privacy, and fallbacks.

## Fields and validation

| Field | Behavior |
| --- | --- |
| `id` | Automatically generated, non-editable UUIDv4 primary key. |
| `case` | Required case foreign key. Deleting the case deletes its people. |
| `first_name`, `last_name` | Optional text, up to 150 characters each. |
| `phone_number` | Optional text, up to 64 characters; preserves international formatting and extensions. |
| `email` | Optional Django-validated email address, up to 254 characters. |
| `address` | Optional multiline address, without country-specific structure. |
| `notes` | Previously saved investigator notes, now read-only in the product and admin. |
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
field, and save. Existing records have read-only case and timestamp fields. **Record dates** is
an expandable Django admin fieldset. Case
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
collaborators use the product interface described above.

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

`people` owns the UUID person table, while `socialmedia` and `geo` own linked
social accounts and POIs. Apply the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema)
to a new database and queue. No data conversion or runtime upgrades are included.

Authorization is enforced by both product and admin entry points, not
automatically by ORM queries. Future exports, background jobs, and integrations
must check current case permissions themselves. Social accounts and POIs are stored
in separate tables linked to each person. Public APIs and broader identity linkage
remain planned.
Person-location evidence provides a reported association, not verified identity
or presence; its immutable person link is case-scoped.
