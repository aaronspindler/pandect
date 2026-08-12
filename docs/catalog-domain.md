# Catalogue and communication domain model

Pandect's catalogue domain is split across focused Django apps. Together they
define the first source-neutral investigation records without adding a source
connector, resolved identity graph, conversation model, search index, or
analytical conclusion.

## Relationships

```mermaid
erDiagram
    CatalogItem ||--o| Image : extends
    CatalogItem ||--o| Video : extends
    CatalogItem ||--o| Event : extends
    CatalogItem ||--o| Note : extends
    CatalogItem ||--o| Message : extends
    Message ||--o| TextMessage : specializes
    Message ||--o| ImageMessage : specializes
    Message ||--o| VideoMessage : specializes
    CommunicationParticipant o|--o{ Message : sends
    Message ||--o{ MessageParticipant : has
    CommunicationParticipant ||--o{ MessageParticipant : receives
    Message o|--o{ Message : replies
    Image ||--o{ ImageMessage : primary_media
    Video ||--o{ VideoMessage : primary_media
```

## App boundaries

- `pandect.catalog` owns the shared `CatalogItem`, common validation and rooted
  deletion behavior, atomic construction services, and the catalogue browsing
  interface.
- `pandect.photos`, `pandect.videos`, `pandect.events`, and `pandect.notes` each
  own their corresponding concrete catalogue record.
- `pandect.messages` owns communication participants, the shared `Message`,
  recipient links, and concrete text, image, and video message records. Its
  media message types reference the standalone records owned by the photo and
  video apps.

The concrete apps depend on `catalog`; `messages` additionally depends on
`photos` and `videos`. No concrete app imports another unrelated concrete app.
This keeps source-neutral catalogue coordination separate from cohesive domain
records without introducing a generic plugin system or registration framework.

The implementation uses explicit one-to-one extension tables instead of deep
Django multi-table inheritance. Every record begins with a `CatalogItem`, whose
`item_type` names the concrete representation. The related object is then
loaded predictably through `item.image`, `item.video`, `item.event`,
`item.note`, or `item.message`; message subtypes continue through
`message.text_message`, `message.image_message`, or `message.video_message`.
This keeps type-specific fields off unrelated tables and requires no
polymorphism dependency or generic foreign key.

## Models and cardinality

- `CatalogItem` is the stable UUID-based chronological record shared by every
  item. Its concrete choices are image, video, event, note, text message, image
  message, and video message.
- `Image` and `Video` hold optional Django storage references and already-known
  media facts. They are standalone catalogue records even when a message uses
  them.
- `Event` records an optional end and description. A known end cannot precede
  its known occurrence time.
- `Note` contains a non-whitespace plain-text observation.
- `Message` contains the fields common to communication: an optional body,
  zero or one sender, zero or more recipients, and an optional direct reply.
- `TextMessage` requires the parent message body to contain non-whitespace
  text. `ImageMessage` and `VideoMessage` allow an empty caption and reference
  one primary standalone media record.
- `MessageParticipant` is the explicit recipient join. A recipient has one of
  `to`, `cc`, `bcc`, `mentioned`, or `unknown` for that link. The database
  prevents the same participant and role from being repeated on one message,
  while allowing the same participant in different roles. `message.recipients`
  therefore returns role-bearing `MessageParticipant` links rather than a
  potentially ambiguous list of distinct participants.

A `CommunicationParticipant` is the identity visible in a communication
context: a displayed name, address, username, phone number, account identifier,
or an incomplete placeholder. It is intentionally not a claim that the record
is a resolved real-world person or organization. Cross-source identity
resolution remains deferred.

## Timestamp definitions

- `occurred_at` is the best-known original time: sent time for a message, start
  time for an event, capture or creation time for media, or an explicitly
  supplied subject time for a note. It remains null when unknown.
- `catalogued_at` is when the item first entered Pandect. It defaults to the
  current time and never substitutes for an unknown `occurred_at`.
- `created_at` and `updated_at` are database-record audit times.

Datetime model validation rejects naive input. With Django timezone support
enabled, stored and returned values are timezone-aware. Default catalogue
ordering is ascending known `occurred_at`, then `catalogued_at`, with unknown
occurrence times last.

Catalogue models run `full_clean()` for ordinary `save()` and `create()` calls,
so model and cross-field validation is applied consistently. Bulk ORM writes
such as `update()` and `bulk_create()` bypass Django model methods and are not a
supported domain write path. Use `create_catalogued_item()` and
`create_catalogued_message()` from `pandect.catalog.services` for atomic
multi-table creation; a validation failure then rolls back the catalogue root
and every extension created so far.

## Media storage and deletion

File fields use Django's default storage beneath `PANDECT_DATA_DIR/media`; the
database stores storage-relative names, not arbitrary absolute machine paths.
An image or video message keeps only a foreign key to its primary media record,
so media file data is not duplicated. The reference uses `PROTECT`: catalogued
media cannot be deleted while any media message still uses it.

`CatalogItem` is the only deletion root. Individual and queryset deletion on an
image, video, event, note, message, or message subtype is rejected, preventing
orphaned chronological records and avoiding different instance/queryset
semantics. Delete the related `CatalogItem` instead; its cascade removes the
extensions. Deleting a message leaves referenced media intact. Replies to a
deleted message are retained with `reply_to` set to null. Deleting a participant
sets sender references to null and removes recipient links. Django does not
automatically delete the underlying stored file when a media database record is
deleted; file removal requires a future explicit, evidence-aware operation.

## Extension points and deferred concerns

An item's explicit `item_type` is immutable after creation. To add a catalogue
type, add an `item_type` choice, place a cohesive one-to-one model in the app
that owns that capability, add its type validation, register it in the
appropriate atomic-construction mapping in `pandect.catalog.services`, add an
app-local migration, and write focused tests. To add a message type, add a
concrete message item choice and a one-to-one extension of `Message` in
`pandect.messages`; update the shared set of valid message types and the message
service mapping. Future mixed or multiple media should use a separate
`MessageAttachment` join rather than adding more nullable attachment columns to
`Message`.

Conversations, threads, arbitrary item relationships, raw source records,
provenance, source-native identifiers, participant resolution, platform fields,
ingestion, search, annotations, and media processing remain intentionally
deferred.
