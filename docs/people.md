# People

People are investigator-maintained case records independent of login accounts.
Matching names or contact details establish neither identity nor a link.

## Investigator workflow

Open **Case Tools → People**. Owners and editors can **Add person** with at
least one name, phone number or email. All case members can browse and, when
enabled, use [hosted search](cloudflare-search.md). Return links retain search
and page during record changes.

**Edit person** corrects name and contact details. Stale edits preserve the
draft beside newer values for **Save reviewed changes**. Records belong to
their original case.

Details show related evidence/findings, populated contacts, social accounts,
POIs, and an unverified-record caption. **Record details** holds saved read-only
notes, name components, and timestamps. Owner-only **Delete person** is visible
below that disclosure and opens the separate confirmation.

Only owners can delete an individual person after named confirmation.
Person locations and communication links block deletion even when archived.
The deletion page links to blocking evidence. Whole-case deletion removes both
the people and evidence. See [location observations](evidence.md#location-observations).

### Related evidence and findings

Details show 25 related evidence records and 25 findings per page, paginated
independently of POIs and People. **Findings** beside the person's name jumps to
the findings section when results are available. Browse links retain the person filter.
The detail view uses active evidence. Sender, Recipient, Both, and Location labels describe
explicit text-message, phone-call, FaceTime, or person-location relationships.
Self-messages appear once; call additions/extractions retain their parent
association and exact source links.

Only a finding's cited sources establish its person association; other sources in
the run do not. Findings follow the full collection's latest-equivalent-run and
review rules. Matching names/contacts, imported participant labels, source exports,
vehicle observations, and POIs do not create evidence associations.

### Writing style

Open **Writing style** on a person record to read that person's saved writing
analyses. Owners and editors can describe the writing in a sample or compare
earlier and later samples. The workflow uses active case messages that record
the person as the sender. See [writing samples by person](analysis-communications.md#writing-samples-by-person)
for date selection, coverage, and interpretation.

### Social accounts

Choose **Add social account** to record a named platform and reported username,
preserving spelling/punctuation. Optional **Source / citation** records the
reported association; ownership remains unverified.

Each exact platform/username pair is allowed once per person. Hosted People
search includes these fields. Viewers inspect. Owners and editors edit or
confirm removal. Stale edits retain drafts, and changed removal targets need
fresh review. Deleting the person or case removes its social accounts.

### Points of interest

**Add POI** preselects the person. Home, Work, and custom labels are investigator
assertions, separate from contact addresses and retained location evidence.
Pointer placement has a keyboard/form alternative. See [maps](maps.md#person-points-of-interest)
for fields, access, pagination, deletion, and privacy.

`geo.PointOfInterest` owns POIs. Deleting a person removes their POIs; retained
evidence links still block person deletion, while POIs add no deletion restriction.

## Fields and validation

At least one first name, last name, phone number, or email must contain a
non-whitespace character, enforced by PostgreSQL even for direct/bulk writes.
Unknown names are allowed; address/notes alone are insufficient.

Each record has one phone number, email, and freeform address. Phone formatting
and extensions are preserved without normalization or numbering-plan validation.
Names/contacts need not be unique within or across cases. Notes remain read-only;
timestamps/admin history are not versioned evidence or identity-assertion history.

The [model](../people/models.py) owns fields/constraints. Trusted ORM callers must
call `full_clean()` for email/field validation; `save()` does not do this automatically.

## Django admin

Use `/admin/` → **People** under the [shared gates](cases.md#django-admin) and
`people` permissions. Deletion requires case ownership, `people.delete_person`,
and no protected evidence links. Existing case, notes, and timestamps are read-only.

## Migration and scope

`people` owns the person table. `socialmedia` owns reported accounts and
`geo` owns POIs and person locations. Use the [fresh migration
baseline](architecture.md#primary-keys-and-the-initial-schema). Jobs, exports
and other ORM callers enforce current case access themselves.
