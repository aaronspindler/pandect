# ADR 0002: Async multimodal evidence processing

- Status: Accepted
- Date: 2026-08-12

> Evolution for the public alpha: native parsing/inference now belongs behind
> the isolated media-runner boundary described in the
> [living overview](overview.md#media-boundary). Trusted publication staging
> remains on the persistent filesystem; decoder scratch is ephemeral in the
> runner. These changes strengthen execution isolation without changing the
> immutable evidence, consent, or single-worker decisions in this ADR.

## Context

ADR 0001 deliberately deferred evidence processing, provenance, and identity
resolution while the local runtime and catalogue were established. Pandect now
needs reproducible extraction from messages, images, video, and audio without
weakening the single-machine privacy boundary or treating generated output as
canonical fact.

## Decision

Pandect adds one cohesive `processing` capability and a closed, operator-owned
`identities` gallery. Intake stores the original first, creates application
database run and stage rows in the same transaction, and hands UUID-only work
to the existing one-worker SQLite Huey queue after commit. Huey is an executor;
application rows are the durable workflow and provenance record.

Every run is bound to the canonical input's SHA-256 and byte length. Results,
timestamp claims, derived artifacts, model observations, consent, and operator
decisions are stored separately with versioned provenance. Processors never
rewrite originals, message bodies, declared media fields, participants, or
titles. A changed source supersedes active work; a rerun creates a new history
entry rather than overwriting output.

The item detail page presents four tiers:

1. canonical and operator-entered facts;
2. deterministic local extraction;
3. explicitly uncertain model-generated hypotheses;
4. operator-confirmed assertions.

Standalone audio becomes a first-class catalogue type. Image and video messages
continue to reference standalone media; their caption is processed separately
and their media is not duplicated. There is no `AudioMessage`.

Local deterministic processing uses bounded argv-only FFmpeg/ffprobe,
ExifTool, Tesseract, Pillow, OpenCV, OpenVINO, and sherpa-onnx capabilities.
Models are digest-qualified local artifacts and are never downloaded by
startup or a task. The maximum-inference manifest includes both FP16 OpenVINO
IR graph/weight pairs; missing or checksum-invalid models produce visible stage
failures rather than silent reduced inference.

Cloudflare Workers AI through AI Gateway is optional and disabled by default.
Remote work requires an immutable per-item manifest recording recipients,
exact source and derivative hashes, selected model and prompt versions, and
planned requests. Consent is invalid when any of those values changes. Gateway
payload logging and caching are disabled; only opaque stage metadata is sent.
Ordinary tests never use a network or credentials.

Merged-transcript semantic analysis and video-account model synthesis are not
chained onto that first consent. They are offered only after a current durable
aggregate succeeds, under a second immutable consent that binds the canonical
source plus exact aggregate result IDs, result hashes, serialized input bytes,
prompt/schema, model, recipients, destination, and request count. Transcript
follow-up input is structured JSON retaining the merged text, source-time word
and utterance records, and scoped speaker/reconciliation labels; its dedicated
output schema cites transcript-text spans and source times. Those stages have
one attempt and never overwrite first-pass or aggregate evidence; a retry
requires a newly reviewed follow-up manifest.

Timestamp extractors preserve raw and conflicting claims. Only explicit-offset
or GPS UTC claims are normalized automatically. `occurred_at` changes only
after an operator promotes a claim, supplying an IANA timezone and DST fold
where necessary.

Face, voice, and writing observations remain modality-specific closed-gallery
evidence. Scores are not probabilities, modalities are not automatically
fused, and confirmation never rewrites canonical participants or facts.

## Consequences

Long processing is checkpointed into durable stages and repaired by a
five-minute reconciliation task after crashes. One worker and one internal
native thread remain the conservative default; work is not high-throughput or
preemptive. Temporary work and local models live under the persistent data
directory rather than the small `/tmp` mount. Worker shutdown uses SIGINT with
a six-minute grace period, while correctness still relies on idempotency.

Video keyframes use an explicit code-owned checkpoint rather than a generic
DAG: a persisted scored scene/coverage plan creates one `frame:NNN` extraction
stage per selection, successful frames independently fan out to analysis, and a
terminal aggregate records successful artifacts plus failed units. Persisted
priorities let the single worker interleave extraction and analysis without
re-running completed siblings.

Backups must include originals, derived artifacts, models needed for
reproduction, both SQLite files, and the generated secret. Embeddings are local
sensitive data: they are neither rendered, logged, nor included in remote
manifests.

## Deliberate boundaries

This decision advances only processing, provenance, standalone audio, and a
closed reference gallery. It does not add source imports, arbitrary
attachments, search or vector indexes, annotations, global entity correlation,
open-world identity lookup, automatic fact checking, reverse geocoding,
authentication, distributed workers, remote storage, or a generic workflow or
plugin framework.
