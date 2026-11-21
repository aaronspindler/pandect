# Evidence analysis implementation roadmap

Future priorities below have no delivery dates or committed capacity.
[Case analysis](analysis.md) and its focused guides own implemented capabilities,
limits and configuration; the [index](README.md#investigation-workflows) routes
to workflows. The [October catalogue](archive/evidence-analysis-2026-10.md) is
historical research, not a current inventory or effort estimate.

## Qualify interpretation and related-passage retrieval

Qualify synthesis on representative sanitized selections: supporting/contrary
passages, negation, cancellation, hearsay, ambiguous references, unknowns and
abstention. Retain model/runtime identities; measure interpretation separately
from quotation agreement. Implementation and mocked tests do not establish quality.

Case-wide search now retrieves source-linked passages with a configured private
embedding model. Continue evaluating paraphrases, negation, uncertain references
and irrelevant hits on representative sanitized cases. Inferred timelines,
entity resolution and graph exploration remain future work.

**Complete when:** investigators can inspect sources for every candidate link or
summary claim and reject suggestions without changing originals. Unavailable
evidence cannot leak through retrieval or generated answers.

## Then: targeted specialist tools

Extend methods for observed needs; catalogue entries are not release commitments.

[Sighting groups](analysis.md#sighting-groups) already use reported source
associations, retained plate comparisons, and retained spatial findings.
Future visual and entity-linking integrations need exact source anchors and
reviewable comparisons before they can contribute connections.

- **Images and plates:** improve supported plate conditions and evaluate
  near-duplicate comparison. Retain ambiguous readings, source regions and
  alternatives; similarity establishes neither the same capture nor owner/driver.
- **Structured documents:** define formats before adding tables, forms and
  transactions. Preserve units, headers, row/column meaning, subtotals and document
  type before aggregation. Native PDF text extraction does not OCR scanned pages.
- **Face grouping/reference comparison:** require quality screening, attributed
  references, false-match evaluation, no-match outcomes, review and a compatible
  configured inference route.
- **Demand-led additions:** translation, speaker separation, QR/barcode decoding,
  document classification and object/scene labels. Prioritize these only for a
  concrete supported workflow.

**Complete when:** representative material needs less review work while exposing
errors, alternatives and source anchors. Evaluate negatives and resource limits
before making quality claims.

## Conditional research backlog

Authorship, reference-voice and handwriting/signature comparison, visual landmark
inference, manipulation triage and video need a concrete use, suitable evaluation
material, compatible processing and useful abstention. They should not delay
source linking.

Evaluate topic/channel confounding for authorship and biometric false matches.
Video also needs retention, decoding, frame/time provenance and operational
bounds; FaceTime records retain no video. Cross-case matching is out of scope.
Advanced road-route inference and broad semantic linking need evaluation beyond
observation grouping.

## Delivery requirements

Follow [AGENTS.md](../AGENTS.md) for integrity, access, provenance, inference and
handoff, and [validation](validation.md) for checks. Acceptance must establish
source access, understandable coverage/uncertainty, rejection without changing
originals, and failure recovery. Planning scores prove neither suitability nor accuracy.
