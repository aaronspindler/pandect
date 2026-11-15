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

The detail page shows related evidence and findings, populated contacts, social
accounts, and POIs, with an
unverified-record caption. **Record details** holds saved read-only notes, name
components, timestamps, and the owner's **Delete person** action.

Only the owner can delete an individual person, after a separate named-target
confirmation. Deleting an unlinked person retains evidence, login accounts, and
other people with matching contacts. A person referenced by retained
person-location evidence or a sender/recipient link in communication evidence
cannot be deleted individually, even if the evidence is
archived. Whole-case deletion still removes both together; see [location
observations](evidence.md#location-observations). Ordinary investigators do not
need Django admin access.

### Related evidence and findings

Person detail shows 25 related evidence records and 25 findings per page, with
independent pagination from POIs and the People list. Browse links open the full
collections with the person filter retained. Archived evidence remains included.
Sender, Recipient, Both, and Location labels describe explicit relationships:
text messages, phone calls, FaceTime records, and person-location observations.
A self-message appears once. Call additions and retained extractions keep their
parent association and exact source links.

Only sources actually cited by a finding determine whether that finding relates
to the person; another source in the same analysis run is insufficient. Findings
use the same latest-equivalent-run and review rules as the full collection.
Matching names or contacts, imported participant labels, source export files,
vehicle observations, and POIs do not create these evidence associations.

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

Choose **Add POI** on a person to preselect that person in the coordinate form.
Home, Work, and custom labels are investigator assertions, separate from contact
addresses and retained location evidence. The map offers pointer placement and a
keyboard/form alternative. See [maps](maps.md#person-points-of-interest) for
fields, permissions, pagination, deletion consequences, and privacy.

`geo.PointOfInterest` owns POIs. Deleting a person removes their POIs but remains
blocked by retained evidence links; POIs impose no additional deletion restriction.

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

Open **People → Add person** in `/admin/` and select an owned/editor-access case.
Case, notes, and timestamps are read-only on existing records; case reassignment
is unsupported. The [shared admin gates](cases.md#django-admin) also apply:

| Operation | Case requirement | Global permission |
| --- | --- | --- |
| List, search, inspect a person or history | Owner, viewer, or editor | `people.view_person` or `people.change_person` |
| Create a person | Owner or editor of the selected case | `people.add_person` |
| Edit a person | Owner or editor | `people.change_person` |
| Delete a person | Owner only | `people.delete_person` |

Case permissions do not grant person permissions. Lists, searches, submitted
case choices, direct/history/deletion URLs remain scoped server-side. Bulk actions
are disabled. Revocation/downgrades affect subsequent requests; superusers have no
case-access bypass. Case deletion in admin also needs `people.delete_person`.

## Migration and scope

`people` owns the UUID person table, while `socialmedia` and `geo` own linked
social accounts and POIs. Use the
[fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).

Authorization is enforced by both product and admin entry points, not
automatically by ORM queries. Future exports, background jobs, and integrations
must check current case permissions themselves. Social accounts and POIs are stored
in separate tables linked to each person. Public APIs and broader identity linkage
remain planned.
Person-location evidence provides a reported association, not verified identity
or presence; its immutable person link is case-scoped.
