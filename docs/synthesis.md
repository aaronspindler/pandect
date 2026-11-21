# Retained question-driven synthesis results

The Selections tool and its **Ask a question** form have been removed. Existing
synthesis runs remain available through their result links and findings. Current
case permissions, source validation, and explicit external approval for
retries/reruns still apply. This guide describes the retained result format and
processing limits; there is no form for creating a new question.

Inputs include captured text/messages, supplied call transcripts, exact retained
extraction/OCR/transcription versions, and explicitly selected findings. Raw
images, recordings and structured location/call records supply no independent
synthesis text. Findings may cite them, but their bytes are not sent. Every
selected source must be usable; none is silently dropped or replaced.

## Citations and interpretation

Original/extracted quotations validate exact character offsets. Prior-finding
quotations validate that finding's label, text and sorted JSON values, retaining
result ID, item index, and quotation offsets. They
appear as quoted derived findings with separate underlying source attribution.

**Supported observations** and **Contrary evidence** are AI suggestions.
**Not established by this selection** contains questions and limitations.
Valid quotation anchors do not establish interpretation accuracy.

Model instructions preserve negation, cancellation, hearsay, ambiguity and
competing accounts, treating imported instructions as untrusted data. Mocked
tests cover structure, quotations, source boundaries, failed publication and
access; they do not qualify model accuracy.

## Bounds, privacy, and recovery

Synthesis inputs allow 500 text/finding records, 500 distinct contributing source
snapshots and 64,000 characters. Narrow oversized selections; they are never
truncated. Output allows 100 observations of 4,000 characters, ten citations per
observation and 20 unknowns of 2,000 characters each, within shared result/inference
limits. Retained dependency depth is limited to eight.

Retained synthesis reruns use their processing route and the selected text and
findings. External reruns require the case owner's current approval of the
provider and model. Raw files are excluded. See
[local analysis](local-analysis.md) and [external AI setup](analysis.md#external-ai-configuration).

Access and contributing originals are rechecked at enqueue, execution,
publication and inspection. Missing sources/prior results withhold downstream
content. Failures retain recoverable runs without publishing invalid suggestions.
Deletion inventories follow prior-finding dependencies.

Run focused validation:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings text.tests.test_synthesis --noinput
```

Separately qualify sanitized selections covering supporting/contrary evidence,
repeated quotations, negation, cancellation, hearsay, ambiguity, prompt injection
and abstention. Record model/runtime identity, latency, resource use, citation
validity and interpretation errors. This guide claims no live qualification or
deployment.

Deliberately run the synthetic text-only harness against the configured private
runtime, without database writes or external AI calls:

```sh
uv run --locked --env-file .env python manage.py evaluate_local_analysis --method question_synthesis > /tmp/synthesis-qualification.json
```

Prior-finding chains and representative case interpretation need separate evaluation.
