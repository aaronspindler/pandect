# Core interface implementation plan

**Status: core interface implementation present; validation results are separate.**
This document records the delivery sequence, architecture, and acceptance gates
for the original product screens. The current workflow, including the Evidence
landing page and read-only saved notes, is in the screen specifications below.
This plan does not assert that a particular local,
CI, browser, or deployed check has passed.
Follow the [design standard](../DESIGN.md) and
[screen specifications](interface-design.md). The selected direction is the
compact three-pane workspace, refined to show more evidence at once.

## Delivery sequence

The implementation follows these six increments. Keep follow-up changes focused
and update relevant user/operations documentation with behavior changes. Preserve
the UUID foundation and existing data. Publication, deployment, and runtime-version
upgrades are outside this delivery. The completion statements below are acceptance
criteria, not a record of successful check execution.

### 1. Shared shell and account experience

- Add shared Django templates and semantic CSS tokens, with local static assets
  and focused vanilla JavaScript. Configure root template/static discovery and
  include these directories in the Docker build allowlist and image. Keep
  `collectstatic` documented; the image collects assets at build time and
  WhiteNoise serves them in production. Never treat evidence as static content.
- Apply the same visual language to allauth sign-in, registration, mandatory email
  verification, password recovery/change, email management, and POST sign-out.
  Preserve allauth's security and validation behavior. Product access requires an
  active authenticated user with a verified email, including existing sessions.
- Make `/` lead authenticated verified users to All cases and others through the
  appropriate account flow. Set successful sign-in to the case list, retaining
  safe local return destinations for invitation acceptance. Switch the landing
  redirects when the real case list is available in increment 2; intermediate
  commits must not redirect users to a missing route.
- Implement compact case navigation, skip links, messages, form errors, loading
  states, light/system-dark appearance, reduced motion, and responsive layouts
  before adding domain-specific visual variants.

**Complete when:** account flows share the shell, normal users need no staff
permissions, assets work in the built image, and keyboard/mobile/no-JavaScript
navigation works.

### 2. Cases and reusable case authorization

- Add `Case.name` (required, 255 characters), optional `description`, and
  `updated_at`. Backfill existing names as `Case #<full UUID>` without changing
  ownership, original creation times, relationships, identifiers, or evidence.
  Initialize the new update timestamp from each case's original creation time.
  Use an additive migration against the existing UUID schema; do not rewrite
  initial migrations or reset databases as part of interface delivery.
- Implement scoped All cases search/list, create, overview, edit, and owner-only
  deletion confirmation. Creation assigns ownership from the request. Editors
  and viewers can inspect case metadata; only the owner can edit it.
- Introduce a small case authorization module used by product views and mutation
  helpers. Scope every parent and child lookup to current access; unrelated users,
  including superusers, receive no case bypass. Validate referenced objects belong
  to the URL's case. Use POST and CSRF for mutations.
- Serialize case mutations and collaborator changes on the case row. Recheck
  authorization inside the mutation transaction. Use the saved update timestamp
  to reject stale edit forms while preserving submitted values for review.
- Case deletion confirmation names the case and counts people, active/archived
  evidence, collaborators, and pending invitations. Require explicit confirmation,
  then apply the existing transactional cascade and post-commit file cleanup.
  Do not promise undo or verified erasure of storage/backups.

**Complete when:** the full case lifecycle works for ordinary accounts, case
boundaries survive forged requests and revoked access, and existing records
survive migration intact.

### 3. People management

- Build case-scoped searchable/paginated person list, detail, create, edit, and
  owner-only deletion confirmation using the existing fields and validation.
  Owners/editors can add and edit; viewers can inspect. Do not offer case
  reassignment, merge, account linkage, aliases, or identity verification.
- Keep names optional and require at least one name, phone number, or email.
  Preserve duplicate contacts as separate records. Label notes as investigator
  input and show the case context consistently.
- Preserve submitted values on validation/conflict errors; compare the saved
  `updated_at` before editing. A deleted record must not be recreated by a stale
  form. Explain that deleting a person does not delete evidence or login accounts.

**Complete when:** the role matrix, partial identities, invalid contacts, duplicate
records, stale forms, direct-object requests, and cross-case attempts are covered.

### 4. Evidence workspace and private originals

- Build one case-scoped evidence collection across the four concrete models.
  Filter/search each authorized type before combining results; paginate a stable
  metadata-only projection rather than loading original bodies/files into lists.
  Default to active evidence, with explicit Archived and All filters. Keep query,
  filters, and page in the URL.
- Use canonical routes containing both evidence type and UUID, beneath a UUID
  case route. Allow only `image`, `audio`, `document`, and `text`; resolve the
  model from an explicit allowlist. A UUID alone never establishes authorization.
- Implement the selected three-pane desktop layout with ordinary evidence links,
  a current-item state, compact provenance, Original/Details navigation, and
  disclosed integrity details. At smaller widths open the same canonical detail
  page. Optional partial navigation must preserve Back/Forward, filters, selection,
  and list scroll; full-page navigation remains complete.
- Add explicit create/edit forms for all four evidence types. Case and uploader
  come from the server; generated integrity fields and originals are read-only.
  Editing covers descriptive/source details and clearly labeled analyst-supplied
  type metadata. Check `edited_at` for stale edits.
- Deliver originals through currently authorized attachment responses with safe
  filenames, `nosniff`, and private/no-store caching. File originals are downloaded
  as `application/octet-stream` attachments, not embedded untrusted documents.
  Escape displayed text; a `text/plain; charset=utf-8` attachment reproduces the
  saved UTF-8 body. Apply private/no-store caching to case-material pages as well
  as downloads. Missing storage produces a recoverable
  error without exposing a private key or backend exception.
- Add archive state on the abstract evidence base: `is_archived=False`, nullable
  `archive_changed_at`, and nullable `archive_changed_by` with protected user
  attribution. Set the latter pair on a real archive or restore transition.
  Owners/editors can perform either action; repeat requests are idempotent.
  Current case members can still inspect and download archived records.
- Keep bulk purge, replacement originals, and an archive-history framework out
  of scope. Archive/restore leave original content, checksum, uploader, and
  creation time unchanged. Ordinary investigators retain evidence until whole-case
  deletion. Superusers with case access may delete individual evidence from
  Details after a named-target confirmation and explicit checkbox. Reuse the
  authorized deletion service and preserve source protections, case isolation,
  rollback-safe file cleanup, and no-JavaScript cancellation.

**Complete when:** all four types can be created, inspected, edited, archived,
restored, and downloaded; permission and integrity tests pass; the dense desktop
workspace meets the row targets without sacrificing accessible controls.

### 5. Sequential upload enhancement

- Keep the standard single-item multipart form and pasted-text form as full-page
  fallbacks. Enhance file selection with a keyboard-accessible queue supporting
  up to 20 files, one request per file, a configurable 100 MiB file limit, and
  1 MiB of UTF-8 pasted text. Show limits before selection.
- Give each queue entry an editable title, suggested but reviewable evidence
  type, optional source/details fields, and a visible state. Stream sequentially,
  show per-file progress, retain successful entries, and allow retry of failed
  entries. Freeze an entry's submitted file/type/details once sending starts.
  Unchanged retries keep that ID/payload; after a confirmed noncommitting
  validation failure, permit corrections and submit the changed entry with a
  fresh ID. Resolve uncertain responses using the original frozen ID/payload
  before allowing edits; a timeout is not proof of failure.
  A failed entry does not discard later entries; lost authentication or write
  access stops the queue.
- Enforce actual byte limits during upload, validate again before saving, and
  document matching request limits/timeouts for the serving proxy. Django's
  memory thresholds alone do not cap uploaded file bytes. Accept one file per
  request so a 20-file queue does not become one enormous request.
- Add nullable immutable unique `submission_id` UUID to the abstract evidence
  base, leaving existing rows null. Each deliberate queue entry gets a fresh
  UUID; ambiguous timeout/retry reuses it. Under the case lock, recheck current
  write access and lookup by actor, case, type, and submission ID, including
  archived records. Return an already committed record rather than creating or
  overwriting one; a conflicting key outside that scope yields a generic error.
  Use the same protection for full-page create forms.
- Keep queue state in page memory, warn before leaving unfinished work, and offer
  **Stop after this file**. Do not persist evidence payloads in browser storage or
  promise resumable uploads. After a page reload/file-input failure, require
  reselection of unsaved files and retain confirmed successes.
- Use `strip=False` for pasted originals, no Unicode normalization, and soft
  textarea wrapping. Explain that the checksum covers browser-submitted captured
  text; use document upload for exact source-file bytes.
- Publish no partially saved record as complete. Preserve the existing documented
  storage/database non-atomicity: failed creation can leave private orphan files;
  do not blindly delete storage on every exception or add a queue service here.

**Complete when:** mixed success/failure, maximum sizes, uncertain responses after
commit, simultaneous retries, deliberate duplicate files, archive-before-retry,
case deletion, and access changes cannot lose confirmed work or duplicate a
single submission.

### 6. Invitations, collaboration, and release qualification

- Add a UUID-keyed `CaseInvitation` model owned by a case, with recipient email,
  viewer/editor role, inviter, timestamps, expiry, token digest, and accepted or
  revoked state. Use a random opaque token and store only its digest. Expiry
  defaults to seven days. Prevent simultaneous outstanding invitations for the
  same case/normalized email; expired invitations may be replaced transactionally.
- Let owners invite, resend, revoke, edit pending roles, change member roles, and
  remove collaborators. Resending or changing an invitation role rotates its
  token. Ownership is fixed and never represented as a membership role.
  Reject invites to the owner/current members; use member-role controls instead.
- Require authenticated POST acceptance with a verified allauth email matching
  the recipient. Lock the case/invitation, recheck validity, create at most one
  membership, and consume the invitation. Existing membership roles remain
  unchanged; invitation acceptance cannot silently upgrade/downgrade them.
  Replay, wrong-account, expired, revoked, and deleted-case paths are explicit.
- Send through the configured mailer with its bounded timeout and a configured
  public origin. Delivery failure leaves an actionable pending invitation rather
  than claiming success. Token pages contain no external assets; logs must not
  record invitation tokens or evidence payloads.
  Explicitly redact token-bearing paths and investigation query strings in
  application, Gunicorn, and proxy access logs, and test that redaction.
  Invitation pages send `Referrer-Policy: same-origin` and private/no-store caching.
- Maintain the reproducible Chromium/WebKit suite with synthetic fixtures.
  `make browser-check` runs `config.tests.browser`; install engines through
  `uv run --locked playwright install chromium webkit`. Exercise the principal create/upload/edit/archive/invite
  journeys at desktop/mobile sizes, keyboard-only operation, semantic labels,
  light/dark appearances, reduced motion, forced colors, zoom, and
  no-JavaScript fallbacks. Capture relevant visual evidence.
- Update setup, permissions, evidence handling, SMTP/public-origin/upload limits,
  static serving, and troubleshooting documentation to describe the implemented
  behavior. Keep planned capabilities separate from delivered ones.

**Complete when:** invitations and role changes enforce current access, all six
increments meet the verification gates below, and ordinary users can complete
their work without Django admin or external AI configuration.

## Verification gates

- Run the documented `uv run --locked --env-file .env make check` against an
  isolated PostgreSQL test database. This includes lint, Django tests/system
  checks, migration drift, lock validation, and Compose configuration checks.
- Run `make compose-smoke` for the asset packaging/container changes and verify
  fresh migrations plus persisted originals. Run `make security-check` when
  dependencies change; update the lockfile through uv, never by hand.
- Cover owners, editors, viewers, unrelated users/superusers, unverified/inactive
  accounts, revoked/downgraded access, forged parent/child identifiers, CSRF, and
  direct downloads. Test database rollback and storage failure with synthetic
  files and temporary storage.
- Assert UUID model keys and route conversion, immutable originals, matching
  archive actor/time state, optimistic edit conflicts, bounded list pagination,
  invitation races/replay/mail failure, and one retained record per submission.
- At `1440 × 900`, show at least 12 complete ordinary evidence rows; at
  `1440 × 1024`, show at least 14. Keep 16px body text, 44px hit areas, and row
  growth for long titles/zoom. Check `768 × 1024`, `390 × 844`, 320 CSS-pixel
  reflow, 200% text resizing, and 400% browser zoom.
- Report local checks, browser inspection, hosted CI, and deployed behavior
  separately. Documentation/mockup delivery alone is validated through content,
  links, and whitespace review, not application tests that restate the documents.

## Boundaries and defaults

This release implements complete core management: accounts, cases, people, retained
evidence, and collaboration. It does not add ownership transfer, self-service
account deletion, person/evidence identity links, extraction, previews, OCR,
transcription, AI, graphs, maps, timelines, reporting, background processing,
resumable uploads, or a general activity/audit framework. The file/text/batch
limits and invitation expiry above are the configurable application defaults.
See [configuration](development.md#configuration) and
[validation](validation.md#browser-checks) for executable setup/check instructions.
