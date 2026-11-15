# People

`people.Person` is an investigator-maintained record belonging to exactly one
`cases.Case`. People are independent of login accounts: a matching name, email,
or phone number is not
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

Names may be unknown. At least one of first name, last name, phone number, or email
must contain a non-whitespace character. A PostgreSQL check constraint enforces
this, including for direct and bulk database writes. Address and notes alone do
not meet this requirement.

There is one phone number, email, and freeform address per record. Phone numbers
preserve formatting and extensions without numbering-plan validation or
normalization. Names and contact details are not unique, either within a case or
across cases. Saved notes remain read-only. Timestamps and Django admin history
are not a versioned evidence or identity-assertion history.

The [model](../people/models.py) owns field definitions and constraints. Trusted
ORM callers must use `full_clean()` for email and field validation before saving;
`save()` does not perform it automatically.

## Django admin

Use **People** in `/admin/` under the [shared admin gates](cases.md#django-admin),
with `people` model permissions. Only case owners with `people.delete_person`
can delete a person, subject to the retained-evidence protections above. Existing
case, notes, and timestamps are read-only; case reassignment is unsupported.

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
