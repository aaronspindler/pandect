# Evidence analysis implementation roadmap

This roadmap owns future priorities, with no delivery dates or committed capacity
assumptions. [Case analysis](analysis.md) and its focused guides own implemented
capabilities, input limits, and configuration; the [documentation index](README.md#investigation-workflows)
routes to current investigation workflows. The [October 2026 catalogue](archive/evidence-analysis-2026-10.md)
is historical research, not a current capability inventory or effort estimate.

## Next: qualify interpretation and retrieve related passages

Synthesis implementation and mocked citation tests do not establish model quality.
Evaluate supporting/contrary passages, negation, cancellation, hearsay, ambiguous
references, unknowns, and abstention on representative sanitized selections.
Retain model/runtime identities and measure interpretation separately from exact
quotation agreement.

Add case-scoped semantic retrieval alongside literal search when evaluation
demonstrates useful results. Preserve index provenance and source selection, check
access at query/result time, and test irrelevant as well as relevant hits. Inferred
event timelines, entity resolution, and graph exploration remain future work.

**Complete when:** investigators can inspect every candidate link or summary
claim against its sources and reject suggestions without changing originals.
Unavailable evidence cannot leak through retrieval or generated answers.

## Then: targeted specialist tools

Extend existing methods for observed case needs instead of treating every
catalogue row as a release commitment.

- **Images and plates:** improve supported plate conditions and add evaluated
  near-duplicate comparison. Retain ambiguous readings, source regions, and
  alternative explanations; similarity does not establish the same capture,
  vehicle owner, or driver.
- **Structured documents:** add tables, forms, and transactions after defining
  supported formats. Preserve units, headers, row/column meaning, subtotals, and
  document type before aggregation. Native PDF text is not scanned-page OCR.
- **Face grouping/reference comparison:** require quality screening, attributed
  references, false-match evaluation, no-match outcomes, and review. A compatible
  configured inference route is a prerequisite.
- **Demand-led additions:** translation, speaker separation, QR/barcode decoding,
  document classification, and object/scene labels. Move these earlier only when
  they are necessary for a concrete supported workflow.

**Complete when:** a tool reduces review work on representative supported
material and exposes errors, alternatives, and source anchors. Evaluate negative
examples and resource limits before making quality claims.

## Conditional research backlog

Candidate authorship, reference-voice comparison, handwriting/signature
comparison, visual landmark inference, manipulation triage, and video need a
concrete use, suitable reference/evaluation material, a compatible processing
path, and a useful outcome that can abstain. They should not delay the next
source-linking milestone.

Evaluate topic/channel confounding for authorship and false matches for
biometric comparison. New video support also needs retention, decoding,
frame/time provenance, and operational bounds; FaceTime records do not retain
video. Cross-case matching remains outside this roadmap. Advanced road-route
inference and broad semantic linking require separate evaluation beyond
observation grouping.

## Delivery requirements

Follow [AGENTS.md](../AGENTS.md) for integrity, access, provenance, inference, and
handoff requirements, and [validation](validation.md) for checks. Feature-specific
acceptance should establish that investigators can find the source, understand
coverage and uncertainty, reject suggestions without changing originals, and
recover from failure. Planning scores establish neither suitability nor accuracy.
