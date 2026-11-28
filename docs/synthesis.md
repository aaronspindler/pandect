# Retained question-driven synthesis results

Open saved synthesis runs from their result links or findings. Ask new
questions in [Evidence chat](analysis.md#evidence-chat).

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

Review negation, cancellation, hearsay, ambiguity and competing accounts in the
cited sources.

## Bounds, privacy, and recovery

Runs accept up to 500 text or finding records, 500 contributing snapshots and
64,000 characters. Narrow oversized selections. Output allows up to 100
observations of 4,000 characters, ten citations per observation and 20 unknowns
of 2,000 characters, within shared processing bounds. Finding dependencies
can nest up to eight levels.

Retained synthesis reruns use their processing route and the selected text and
findings. External reruns require the case owner's current approval of the
provider and model. Raw files are excluded. See
[processing boundaries](local-analysis.md) and [external AI setup](analysis.md#external-ai-configuration).

Access and contributing originals are rechecked at enqueue, execution,
publication and inspection. Missing sources/prior results withhold downstream
content. Failures retain recoverable runs without publishing invalid suggestions.
Deletion inventories follow prior-finding dependencies.

The focused suite mocks Gateway responses and checks citation validation, source
boundaries, access and failed publication.

```sh
uv run --locked --env-file .env python manage.py test --settings=config.tests.settings text.tests.test_synthesis --noinput
```

Use representative sanitized selections to evaluate interpretation separately
from citation agreement. See the [roadmap](evidence-analysis-roadmap.md#qualify-interpretation-and-related-passage-retrieval).
