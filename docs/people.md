# People

`people.Person` is an investigator-maintained record in exactly one `cases.Case`,
independent of login accounts. Matching names, emails, or phone numbers do not
prove identity or automatically link/merge records. These records are investigative
work, separate from verified evidence and model output.

## Investigator workflow

Open **Case tools → People**. Owners/editors choose **Add person**, enter at least
one name, phone number, or email, and save. Viewers inspect. Search and every
detail/edit/delete route remain case-scoped. Adding, editing, or deleting a
record retains the People search and page in return links.

**Edit person** corrects name/contact details. Stale forms preserve the draft
beside newer values and require **Save reviewed changes**; they cannot recreate
a deleted record. Case reassignment, identity verification, and automatic merging
are unsupported.

Details show related evidence/findings, populated contacts, social accounts,
POIs, and an unverified-record caption. **Record details** holds saved read-only
notes, name components, and timestamps. Owner-only **Delete person** is visible
below that disclosure and opens the separate confirmation.

Only owners can delete people, after named-target confirmation. Unlinked-person
deletion retains evidence, login accounts, and matching people. Retained
person-location evidence or communication sender/recipient links block deletion,
including when archived. The deletion page links to blocking originals before
offering any confirmation. Whole-case deletion removes both; see [location
observations](evidence.md#location-observations). Product users need no staff access.

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

### Social accounts

Choose **Add social account** to record a named platform and reported username,
preserving spelling/punctuation. Optional **Source / citation** records the
reported association; ownership remains unverified.

People may have multiple usernames per platform, with each exact platform/username
pair allowed once per person. People search includes platform/usernames within
the case. Viewers inspect/search; owners/editors edit or confirm removal. Stale
edits preserve drafts beside current values. Changed removal targets require fresh
review/confirmation. Accounts are managed in the product and deleted with their
person or case.

`socialmedia.SocialAccount` owns the table; forms, views, templates, and tests live
in `socialmedia`. Person details display accounts through that relationship.

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

`people` owns the UUID person table; `socialmedia` and `geo` own linked accounts
and POIs. Use the [fresh migration baseline](architecture.md#primary-keys-and-the-initial-schema).

Product/admin enforce authorization; ORM queries do not. Exports, jobs, and
integrations must check current case access themselves. Public APIs and broader
identity linkage remain planned. Immutable, case-scoped person-location links
record reported association, not verified identity or presence.
