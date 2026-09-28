# Evidence processing

See the [current architecture](architecture/overview.md) for the trusted
orchestrator/media-runner boundary and [SECURITY.md](../SECURITY.md) for its
limitations. This guide describes evidence behavior, not a certification of
forensic admissibility or model accuracy.

## Durable workflow

Supported catalogue intake creates a `ProcessingRun` and explicit
`ProcessingStage` rows before the surrounding transaction commits. The commit
callback sends only a stage UUID to Huey. Tasks claim rows conditionally, do
tool or model work outside a database transaction, re-hash the source, and then
append results in a short transaction. Duplicate delivery is harmless.

Run-state derivation locks the run and its stage snapshot in one short
transaction. On SQLite it first acquires the database's single-writer fence, so
a stale status calculation cannot overwrite a newer claim or terminal
supersession; repeated terminal calculations preserve the original
`finished_at`. When a local task observes replacement source bytes,
supersession of all stale stages and creation (or reuse) of the current-source
run commit atomically. Only its UUID-only Huey dispatch is deferred until that
commit. The reconciler also repairs bounded legacy `source_changed` rows that
have neither an explicit replacement nor another full local run for the current
source. Remote runs are stopped on source change but are never recreated
without new item-specific consent.

Run and stage records—not Huey result values—drive the item-detail status UI.
The periodic reconciler repairs queued work that never started and expired
leases while the two-attempt local limit remains. The same bounded management
command also schedules any remote aggregate omitted by a crash after the final
remote result committed and resumes a lost or expired reference-gallery rematch
cursor. Before an expired or transient attempt is exposed for retry, its exact
publication journal and private workspace are resolved behind the attempt
fence; terminal cleanup also revisits journal rows from older attempt numbers
without deleting a committed `DerivedArtifact`.

Video keyframes use an explicit three-step checkpoint. The `plan` stage first
persists the chosen stream, duration, scored scene candidates, endpoint/scene/
coverage reason, timestamp, and `frame:NNN` unit key for every selection. One
durable extraction stage then publishes each planned frame independently. Its
successful commit immediately creates the matching frame-analysis unit, with
priorities arranged so analysis of one published frame can run before the next
extraction. The keyframe `aggregate` waits until every planned extraction is
terminal, retains successful artifacts, and records a partial result when any
unit failed; retrying one unit never repeats a successful sibling.

Audio splitting uses the same narrow durability principle through an explicit
code-owned map into `stream:N/chunk:NNN` stage units. Each unit is independently
claimable, retryable, and recoverable. Static aggregate rows combine successful
unit results by identifier and record failed unit keys. This remains
deliberately narrower than a generic database DAG or plugin workflow, and the
single worker is preserved.

Each video audio stream produces two distinct local derivatives. The
`audio_stream` artifact is source-faithful: FFmpeg copies packets into a safe
codec container when supported, or losslessly re-encodes the decoded stream as
WavPack while explicitly preserving the probed sample rate and channel count.
Its metadata retains the absolute source stream index, source/output codecs,
profile, sample format, channel layout, extraction mode, and derivative hash.
A separate `normalized_media` FLAC is mono 16 kHz input for local analysis.
Neither whole-stream artifact is eligible for remote transfer; consented
transcription can select only bounded `audio_chunk` derivatives.

Keyframe artifacts retain their absolute source video-stream index as well as
the representative-selection ordinal and requested seek time. They also bind
back to the immutable plan result and retain the selection reason, scene score,
score field, and threshold. The current bounded one-frame FFmpeg invocation
does not expose a dependable absolute decoded PTS or source frame number across
codecs and containers, so those fields are recorded as `not_available` with
explicit semantics; Pandect does not mislabel the selection ordinal or seek
request as a decoded coordinate.

Before a frame or audio-chunk unit reads its persisted derivative, Pandect
recomputes both its SHA-256 and byte size. It repeats that check after analysis
and immediately before appending the outcome. A missing or changed derivative
fails that isolated unit rather than recording results against stale provenance.

```console
make manage ARGS="reconcile_processing"
make processing-backfill ARGS="--item-type image --limit 50"
```

The catalogue backfill action examines at most 50 items in stable
`catalogued_at`/ID order and carries a signed continuation cursor to the next
request. It does not create another run when an item already has a full local
run for the current source hash, byte size, processor version, and complete
configuration snapshot. Repeated actions therefore advance beyond the first
50 items without an unbounded scan; the adjacent restart link deliberately
rechecks earlier positions after an old source or configuration changes. The
item-detail rerun and explicit command remain versioned operator actions and
continue to create a fresh run when requested.

Manual reruns and retries preserve every previous run. Message body edits create
a new input-bound run; title and occurrence-time edits do not. A blank caption
is normalized to empty, supersedes unfinished local and consented remote caption
work, and does not create a replacement run. Referenced image/video media is
processed only through its standalone item. The detail page selects current
evidence only after freshly matching both the canonical SHA-256 and byte size.
Its default base is the newest compatible full local run; the newest compatible
remote, follow-up, and per-modality identity-rematch runs supplement that base
instead of hiding its deterministic extraction. An explicit history selection
isolates exactly the requested run and still labels incompatible evidence as
historical.

## Storage and resource limits

- Canonical files remain under `media/catalog/`.
- Canonical image, video, and audio storage pointers are immutable after model
  creation. Ordinary instance, queryset, and bulk writes cannot repoint an
  original; any future sanctioned replacement must be an explicit service that
  records provenance and creates a new input-bound run.
- Derived files belong under `media/derived/<item>/<run>/` and are never used to
  replace an original.
- Ephemeral work uses `PANDECT_WORK_DIR` (default `/data/work` in Compose), not
  `/tmp` in the trusted services. It holds upload/publication staging; native
  decoder jobs use separate ephemeral runner scratch with no canonical-data
  mount. Django streams larger request uploads to its private `uploads/`
  subdirectory before canonical storage.
- Work and media storage must remain on the same filesystem. Each derived-file
  publication is journaled by stage attempt before Pandect creates a hard link
  into `media/derived/`; file contents and the destination directory are synced
  before the result transaction can commit. This lets reconciliation prove and
  remove only an abandoned attempt's bytes after process or power loss, while
  exact preexisting/recovered files and mismatches are preserved.
- Model artifacts use `PANDECT_MODELS_DIR` (default `/data/models`). Tasks do
  not download weights.
- Uploads are counted while streaming, independently of `Content-Length`:
  2 GiB per file, 8 GiB per batch, 50 files, and a 2 GiB free-space reserve.
  Storage is checked again before original/derived publication. Rejected
  batches must not leave partial catalogue records or provisional originals.
  Django checks request origins and CSRF cookies before multipart parsing;
  upload-limit errors still return 413/507 if parsing stops before the form token.
- Native tools, pixel/audio decoders, and inference execute through fixed
  media-runner operations, not directly inside web or worker. The runner has
  no IP network, application data, credentials, or database access. Trusted
  orchestration verifies returned structures/files and computes final hashes.
- The runner permits one active job. Imports requiring a probe may return
  busy with `Retry-After`; processing stages use their existing bounded retry
  policy for the same contention. Unavailable confinement never falls back to
  direct execution. Its hard defaults are 8 GiB memory/no extra swap, 4 GiB scratch
  with 8,192 inodes, two CPUs, 128 processes/threads, and 256 open descriptors.
  Output is capped at 1,024 regular files and 1 GiB total per job (also 1 GiB
  per file). Core dumps are disabled. Accepted upload size does not guarantee
  a file fits these decoding/output limits.
  Voice analysis allows 3,000 seconds per job: the full supported 600-second
  chunk's decoding budget (`2 × duration + 300s`) plus the same allowance for
  inference, within the runner's two-hour hard limit.
- Trusted response validation also caps a source at seven days' duration and
  64 streams, chapter/scene collections at 10,000 entries, and model output at
  1,024 observations with at most 4,096 dimensions per embedding. Invalid,
  non-finite, malformed, or over-limit results fail the analysis while
  preserving the canonical original. Image source-unit/pixel limits below
  continue to apply inside these response bounds.
- Images are limited to 200 megapixels per decoded frame/page, 100 source
  units, and 200 megapixels in aggregate decoded work per stage. Crossing the
  unit-count or aggregate-pixel boundary truncates at the last fully approved
  unit and records the exact processed indexes and reason.
- Attached-picture streams with complete positive ffprobe dimensions are
  checked against that pixel ceiling before FFmpeg may extract or decode them.
  Oversized artwork is skipped with a durable warning. If the container does
  not declare both dimensions, extraction may proceed, but the artifact records
  that limitation and the bounded post-extraction image preflight must succeed
  before OCR or QR decoding runs.
- OCR, QR, and standalone-image face analysis each preflight the canonical
  image once, then render every approved frame/page to a metadata-free,
  single-unit scratch PNG before invoking the downstream tool or local model.
  Results retain each source-unit index, rendered geometry and hash, exact
  processed indexes, and truncation provenance. OCR adds per-unit blocks,
  lines, words, boxes, and confidence; QR geometry and display-safe face
  observations are unit-scoped. Face embeddings remain opaque local identity
  evidence and never enter the rendered result payload.
- A recognized HEIF/HEIC/AVIF container without a registered local pixel
  decoder keeps its ExifTool metadata result while OCR, QR, literal-visual, and
  face stages are explicitly marked skipped. Damaged supported image formats
  remain failures rather than being misreported as decoder limitations.
- Video analysis is capped at 120 local representative frames and 12 consented
  remote descriptions.
- Exact original-image transfers are capped at 10 MiB by the current JSON-array
  vision adapter to bound request-memory amplification. Larger originals fail
  visibly and are never silently resized or stripped.
- The current direct Nova-3 transcription adapter declares a fixed 25 MiB audio
  payload ceiling. After each mono 16 kHz FLAC chunk is encoded, Pandect checks
  its actual bytes and deterministically bisects any oversized chunk from the
  canonical source. Provider-driven hard cuts use the exact one-second overlap,
  retain source offsets, resampling/downmix parameters, and hashes, and stop
  after eight split levels or 64 encodings per initial chunk. A segment that
  still cannot fit fails locally; both the consent planner and HTTP adapter also
  refuse over-limit chunks.
- Acoustic results expose integrated loudness, loudness range, true/sample
  peaks, full-scale RMS, clipping proxies, silence, and VAD-derived speech and
  non-speech regions. These are measured observations, not content labels.
- Subprocess output is capped at 16 MiB. ffprobe/ExifTool use 120-second
  limits, Tesseract 60 seconds per frame/page, and FFmpeg uses twice media duration
  plus five minutes, capped at two hours.
- The worker remains single-process and native OpenMP, OpenBLAS, MKL, and
  NumExpr runtimes are capped at one thread. Every FFmpeg media invocation also
  pins decoder, encoder, and filter execution to one thread so a single stage
  cannot silently multiply host CPU concurrency.

Run `make processing-diagnostics` to execute isolated bounded binary-version checks and
an actual write, file-fsync, and atomic-rename probe inside the work directory.
It also verifies every manifest digest and exact declared size. Health and
readiness do not depend on models or Cloudflare, but the media-runner must be
available for operations that require it.

Model provisioning is an explicit operator action; startup and processing tasks
never fetch weights. Review [artifact-specific license disclosures](model-licenses.md),
especially the expression model's missing model-local license and unresolved
training-corpus rights. Upstream license labels do not establish commercial
clearance:

```console
make manage ARGS="provision_processing_models --list"
make models-provision ACCEPT_MODEL_LICENSES=yes
make model-smoke
```

The provisioning command downloads only HTTPS URLs in the source-controlled
manifest, bounds each response, writes it under the work directory, fsyncs it,
and verifies its SHA-256 and declared size. Before any selected weight is
installed, its hash-bound upstream license/NOTICE text is downloaded and
atomically archived under `models/legal/`. Legal filenames are content-addressed;
an existing valid copy is reused, while any conflicting file is preserved and
causes a sanitized failure even when weight replacement was requested. Weights
are then atomically renamed into the model directory. Provisioning refuses to
overwrite an invalid weight unless the operator also passes
`ARGS="--replace-invalid"`.

The model notice is an append-only provisioning and license-acceptance record:
partial provisioning appends a new atomic record without discarding earlier
records or operator annotations. Each record includes revisions, SPDX summaries,
exact archived legal-text paths/source URLs/digests, model sources and hashes,
notes, and an artifact-specific statement of what is and is not defensibly known
about the training corpus. The same model and legal provenance is frozen into
every applicable local processing run. Neither core tests nor runtime tasks
download weights or legal text. `make model-smoke` additionally starts a disposable,
120-second-limited isolated runner job, revalidates every manifest digest and size,
and performs real inference using only generated test signals: YuNet detection,
SFace and expression features, both FP16 OpenVINO IR pairs, Silero VAD, and a
CAM++ speaker embedding. The synthetic face pixels and three-second waveform
contain no catalogue evidence and the network-disabled job does not transfer them.
Native runtimes are pinned to one thread, output is bounded, and
missing, incompatible, timed-out, or invalid-output models fail with sanitized
codes. This optional smoke gate is deliberately outside `make check` and needs
no credentials or network access after provisioning. The Python bindings and
their native `sherpa-onnx-core` runtime are pinned to the same release. Compose mounts
models read-only for the runner and trusted services; only the explicit one-shot
model provisioner mounts them writable. Setup/provisioning makes only verified
manifest weights and legal texts readable to disposable job identities, leaving
the surrounding data and annotated notice private. It does not recursively
widen unknown model-directory contents.

## Evidence interpretation

Deterministic extraction and model output remain separate from direct catalogue
fields. Raw metadata, normalized forms, source hashes, tool/model versions,
configuration, uncertainty, and timestamps are retained. File mtime is
operational metadata and not an original occurrence time.

Voice-window quality gating includes a deliberately narrow, deterministic
`possible music signal pattern` heuristic. It uses only the existing mono
16 kHz waveform selected by local VAD; it does not use CAM++, a music model, a
remote service, or inferred content. Revision
`bounded-tonal-stationarity-v1` samples at most 32 evenly distributed
512-sample Hann-windowed frames (256-sample hop), requires at least eight usable
frames, 1.5 seconds of evidence, and 0.01 window RMS, and examines only
80-4000 Hz. Its bounded features are median spectral flatness, median
top-three-bin concentration, dominant-bin stability, mean spectral flux,
spectral-centroid coefficient of variation, and frame-energy deviation. The
exact feature values and every threshold are saved with result provenance.

A possible pattern requires both conservative tonal thresholds (flatness at
most 0.12 and concentration at least 0.50), at least two stationarity cues, and
at least one spectral-temporal cue (low flux or stable centroid); it downgrades
the window and caps its quality score at 0.60. Rejection is reserved for a
strong pattern (flatness at most 0.05, concentration at least 0.70, and at least
three stricter stationarity cues) and is still labeled only as a heuristic
signal pattern. Too little, too quiet, or unusable evidence is `not_assessed`;
`not_flagged_by_heuristic` is not evidence that music is absent. Sustained
speech, alarms, and tones can share the measured cues, so the result is never
presented as a probability, a music classification, or a statement that music
is present or dominant. CAM++ embeddings remain opaque and are computed only
for windows that pass the rejection gates.

Identity matching is local and closed-set. At least two approved distinct face
references, three approved voice clips totalling 30 seconds, or three selected
messages totalling 500 words are needed before a subject is eligible. At most
five candidates plus `unknown` are shown. Face, voice, and writing scores are
not comparable and are never automatically fused.

Processing results, derived-artifact rows, timestamp claims and decisions,
remote consents, and identity observations are create-only evidence records.
Their default managers reject queryset updates, bulk updates, and deletion, and
their instances reject subsequent saves and deletion. Candidate and reference
decision-history events use the same append-only boundary. New interpretations
are represented by new rows and runs rather than rewriting prior evidence.
Their catalogue/run/stage parent relationships use protected deletion rather
than cascades that could bypass instance/queryset guards. Unprocessed catalogue
items remain deletable; items with immutable processing/identity evidence must
retain that history. These are supported application/ORM protections, not a
barrier to a trusted operator directly editing SQLite or files.

The public-alpha execution-policy version is included in new run provenance.
Prior results remain stored and can be shown as historical when they no longer
match current processing configuration. An explicit rerun appends a new record;
upgrading does not rewrite previous conclusions.

Eligibility counts source evidence, not processing executions: a rerun of the
same face or message counts once, and overlapping voice intervals are merged
before clip counts and duration are calculated. Every candidate set records its
producing processing run and stage. The detail page exposes only the newest set
compatible with the current source and live reference-gallery fingerprint;
older sets remain in run history and cannot be confirmed while stale. Every
confirmation and rejection also appends an immutable decision-history event.
Selecting, reassigning, or removing a subject reference likewise appends an
immutable event containing the observation/reference IDs, subject IDs and label
snapshots, notes, previous assignment where applicable, and decision time. A
later edit or removal can change the live gallery row but cannot rewrite or
delete those prior operator decisions; the detail page exposes their history.

Because one reference can change the gallery fingerprint for observations on
other catalogue items, each reference event also creates a durable global
rematch cursor. The cursor examines at most 25 stable item IDs per task, includes
only quality-approved, source-current, gallery-eligible observations of the
same modality, and creates one bounded identity-only run for every affected
item. Each run records the decision and cursor IDs. Replay after interruption
reuses an already-created run rather than duplicating it, and the five-minute
reconciler resumes pending or expired cursors. Existing candidate sets become
non-actionable immediately when their live gallery fingerprint changes; they
do not remain confirmable while replacement runs are queued.

Reference observations are likewise bound through their originating local
processing run to the canonical item's SHA-256 and byte size. If a message body
or media file is replaced, its prior face, voice, and writing observations and
subject-reference rows remain historical evidence, but disappear from current
reference selection, cannot be added or reassigned, and do not participate in
new gallery rankings. Restoring or reprocessing the current source creates new
versioned observations; it never reactivates a mismatched derivative by title,
filename, or observation hash alone.
For videos, each keyframe's face feature remains durable internal evidence, but
only the correlated aggregate face track is gallery-facing, reference-eligible,
and ranked. Repeated appearances of one face therefore cannot manufacture
additional reference eligibility or duplicate candidate sets.

Expression output means apparent facial-expression class only. Adult apparent
age is a broad model estimate. Binary visual-class scores are not sex or gender
identity. Pandect does not infer protected characteristics, truthfulness,
liveness, or open-world identity.

## Optional Cloudflare transfer

Remote processing is disabled unless `PANDECT_CLOUDFLARE_AI_ENABLED=true` and
the account ID, gateway ID, and an account-scoped token with Workers AI Read are
configured. AI Gateway management permissions are needed only when creating or
configuring the gateway. Each item requires fresh consent to the exact manifest:

- message: exact body text;
- image: exact original bytes, including all embedded EXIF, XMP, IPTC, GPS,
  maker-note, thumbnail, and other metadata bytes;
- video: metadata-stripped keyframes and normalized audio chunks only;
- audio: normalized audio chunks and selected embedded artwork only.

The consent page must display the notice version, recipients, model IDs,
requests, bytes/duration, billing and availability caveats, and the
no-payload-logging policy. The submitted form carries both the manifest hash
and the exact notice version that was displayed; a stale page cannot accept a
new notice silently. The signed manifest also binds an operation-specific
adapter/privacy contract: endpoint form, request representation, model options,
provider parameters, retry limit, total/connect/gateway timeouts, response-size
limit, opaque metadata fields, payload-logging policy, and cache policy. Any
source, derivative, model, prompt, notice, adapter/privacy contract, recipient,
or request-plan change invalidates it. There is no bulk consent.

The Nova-3 adapter requests language detection, diarization, utterances,
punctuation, and filler retention. Pandect accepts the provider response only
when each channel includes the detected language and its confidence, each
alternative and word includes transcription confidence, every word has source
start/end times and a chunk-local speaker number, and every utterance has its
channel, source times, confidence, words, and speaker number. Missing requested
evidence fails the isolated stage instead of producing a deceptively complete
transcript.

For video, Pandect independently describes at most 12 representative keyframes
and transcribes the exact normalized chunks listed in the manifest. Each result
retains its frame or source-time location. After every consented request unit is
terminal, a separate durable local stage shifts chunk timestamps to source time,
deterministically reconciles exact one-second overlaps, and retains conflicting
words and confidences as alternatives. Provider speaker numbers remain
chunk-local by default. A local deterministic pass may add a stream-scoped
cluster label when approved CAM++ observations from the exact consent-referenced
local run and audio-chunk hash overlap a timed provider speaker group. It requires
a quality score of at least 0.65, one second of voice evidence, at least 0.25
seconds and 50 percent observation overlap with a 0.20 assignment margin, exact
feature/model/preprocessing compatibility, and complete-link cosine similarity
of at least 0.80 with a 0.05 cluster-ambiguity margin. These labels are local
similarity hypotheses, not identities or probabilities; cross-stream, ambiguous,
incompatible, and unmatched groups keep their original chunk-local labels.
Operator-confirmed voice assertions are retained separately and take precedence
where their timed intervals overlap.

For video, that same no-network stage parses local SRT, WebVTT, and ASS subtitle
derivatives and compiles a timestamped account from keyframe descriptions,
subtitles, OCR, and the merged transcript. The account preserves source result
and artifact identifiers and is explicitly a deterministic composition of
model-generated hypotheses and local extraction, not a new model conclusion.
A QR observation retains its keyframe time, artifact/content hash, result ID,
decoded payload, geometry, and decoder uncertainty. Face evidence is included
only after frame observations have been correlated into an aggregate track; raw
frame or aggregate embeddings are never serialized. Each track retains its
sample times, keyframe/result IDs and regions, apparent-expression score
distributions, broad adult apparent-age bands, anonymous `class_0`/`class_1`
visual-score distributions, and head-pose ranges. These remain separately
labeled model hypotheses and are never presented as emotion, intent, truth,
sex, gender identity, protected traits, or open-world identification. A current
closed-gallery candidate set may contribute local subject labels, raw
similarities, and explicit operator decisions alongside `unknown`; stale
gallery/source sets are omitted.

Every local result, keyframe/subtitle artifact, aggregate face observation, and
current identity-candidate snapshot actually used by the account is bound into
the aggregate result's input hashes and provenance. Artifact bindings include
both the derivative content SHA-256 and a hash of its offsets and metadata.
Follow-up plan construction and consent validation re-read those bounded local
records, re-hash derivative bytes, and re-check that candidate sets are still
source/gallery-current. A post-aggregate result, artifact, subject label,
candidate decision, assertion, observation, or gallery change therefore
invalidates later synthesis before transfer; the same check runs again after a
remote response and before it can be persisted. Empty, unreferenced, or raw
feature records are not added merely because they exist.
A five-minute reconciler repairs the commit-to-aggregate dispatch gap; all
original per-chunk and per-frame results remain immutable.

Once that durable aggregate succeeds, the detail page can offer a distinct
follow-up consent manifest. Nothing is sent automatically. The manifest binds
the current canonical SHA-256 and size, exact aggregate run, immutable input
result IDs and canonical result hashes, exact serialized merged-transcript JSON
(text, stream-local word and utterance timing, confidence, speaker scope and
local reconciliation labels) and/or timestamped timeline bytes, operation,
model, prompt/schema revision,
recipients, destination, and one or two planned requests. Each serialized
follow-up payload is capped at 16 MiB before consent. A separately
consented follow-up run may apply the dedicated transcript-analysis schema to
that structured input, producing source-cited speaker-turn summaries,
conversation structure, speech-style observations, topics, entities, claims,
questions, commitments, important source timestamps, and uncertainty. Provider
speaker numbers remain scoped exactly as recorded; the model may not silently
fuse them. For video, the second request can synthesize a source-indexed account
from the exact timeline. Both prompts treat evidence as untrusted input;
transcript character spans and source-time ranges, plus video source entry
indexes, are validated locally. Source, aggregate result, model, prompt, or plan
changes invalidate consent before and after the call. Each stage has one attempt,
so any retry requires reviewing and consenting to a newly built follow-up
manifest. Initial per-frame, per-chunk, and local aggregate outputs are never
overwritten.

Gateway requests set payload collection off, skip cache, ignore environment
proxy settings, and use a ten-second connect plus a monotonic total client
deadline. Decompressed response bodies are streamed into a fixed 16 MiB limit
before JSON decoding. Initial request units allow at most two Gateway attempts;
separately consented aggregate follow-ups allow only one. Huey does not retry
remote requests. Unknown-outcome failures require an explicit manual retry
because a hidden retry could duplicate cost. Consent prompt hashes include the
exact JSON schema appended by the semantic and vision adapters, so changing a
schema invalidates prior consent.

`make cloudflare-smoke` is an explicit, evidence-free network/cost diagnostic.
It uses the Workers AI model-search API to validate the token and every selected
adapter model, then sends a fixed three-token prompt through the configured
universal Gateway endpoint to prove that gateway/model execution works. It
never reads a catalogue item. Meta license acceptance is a separate legal/state
change and cannot happen during that ordinary diagnostic. After reviewing the
Meta license and acceptable-use policy, an operator may explicitly opt in with:

```console
make cloudflare-smoke ACCEPT_META_LICENSE=yes
```

That form first submits Cloudflare's documented `prompt: agree` request and
then runs the same evidence-free Gateway check. Diagnostic requests disable
payload collection and caching, attach only a fixed diagnostic marker, use one
Gateway attempt, and never print credentials or response bodies.

Cloudflare receives the evidence described above and opaque run/stage IDs.
Usage, cost, status, and timing metadata may remain available even when payload
logging is disabled. Availability, pricing, model licenses, and provider
retention policies can change independently of Pandect; verify them before
consenting. Pandect's adapters are designed not to log credentials or payloads;
review local logs before sharing, and do not infer provider-side retention
guarantees from that application behavior.

## Adding a stage

Add a concrete stage kind, place its small extractor in `processing/extractors`,
wire it explicitly in the item-type map and dispatcher, return typed
`StageOutcome` values, and add hermetic parser plus task-state tests. Record an
input hash and exact tool/model/preprocessing revision. Native parsing and
inference must use a fixed isolated runner operation, including postprocessing
and diagnostics; validate returned data before trusted publication. Do not add a generic DAG,
plugin registry, implicit download, network fallback, or canonical-field write.
