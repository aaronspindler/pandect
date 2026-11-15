# Question-driven synthesis

An owner or editor can open **Case tools → Selections**, choose a shared
selection, and **Ask a question**. The question is limited to 2,000 characters.
The exact selection revision is checked before enqueueing; later collection edits
do not rewrite the run. Viewers can inspect retained results but cannot start work.

Sources are original captured text/messages, supplied call transcripts, exact
native extraction/OCR/transcription versions, and explicitly selected findings.
Raw images, recordings, and structured location/call records are not independent
synthesis text. A selected finding may cite those originals, but their bytes are
not sent to the model. Every selected source must be usable; nothing is silently
dropped or replaced by a newer result.

## Citations and interpretation

The input distinguishes original/extracted text from prior findings. Original
quotations validate exact character offsets. Prior-finding quotations validate a
deterministic surface containing that finding's label, text, and sorted JSON
values; they retain result ID, item index, exact quotation/offsets, and review
state at execution. They are displayed as quoted derived findings, not quotations
from originals. Their underlying source attribution remains available separately.

Results separate **Supported observations** and **Contrary evidence**, both labeled
AI suggestions. **Not established by this selection** holds bounded suggested
questions/limitations, not additional source-backed observations. Selecting a
finding never accepts it. Current reviews and retained review reasons remain
independent of synthesis, and valid quotation anchors do not establish that an
interpretation is true.

The model instruction explicitly preserves negation, cancellation, hearsay,
ambiguity, and competing accounts. Imported instructions are untrusted data.
Routine tests mock inference and validate structure, exact quotations, source
boundaries, failure publication, and access. They do not qualify model accuracy.

## Bounds, privacy, and recovery

At most 500 selected text/finding records and 500 distinct contributing source
snapshots are supported, within 64,000 selected characters. Oversized selections
must be narrowed; they are not truncated. Output is limited to 100 observations,
4,000 characters per observation, ten citations per observation, and 20 unknowns
of at most 2,000 characters each, plus the shared result/inference limits.
Retained synthesis dependencies are checked to a maximum depth of eight.

Configured operator-controlled private inference is the default. Selecting the
external service requires explicit approval and uses Cloudflare AI Gateway.
The approval describes transfer of selected text/findings and review context;
raw files are excluded. No automatic cloud fallback exists. See
[local analysis](local-analysis.md) for identities, capacity, and qualification.

Case access and contributing originals are rechecked at enqueue, execution,
publication, and retained-result inspection. Missing prior results or sources
withhold downstream content. Failures retain recoverable runs without publishing
invalid suggestions. Deletion inventories follow prior-finding dependencies.

Run focused validation with:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings text.tests.test_synthesis --noinput
```

Qualify the configured model separately with sanitized selections covering
supporting/contrary evidence, repeated quotations, negation, cancellation,
hearsay, ambiguity, prompt injection, and abstention. Record model/runtime identity,
latency, resource use, citation validity, and interpretation errors. These workflows
do not claim live model qualification or deployment.

The existing synthetic qualification harness can exercise the text-only synthesis
path without database writes or Gateway calls:

```sh
uv run --locked --env-file .env python manage.py evaluate_local_analysis --method question_synthesis > /tmp/synthesis-qualification.json
```

This requires the configured private runtime and must be run deliberately.
Prior-finding chains and representative case interpretation need separate evaluation.
