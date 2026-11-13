# Evidence analysis implementation roadmap

This roadmap prioritizes future work from the [analysis catalogue](evidence-analysis.md).
The catalogue's October 2026 scores are historical qualitative judgments, not
current effort estimates or accuracy measurements. There are no delivery dates
or committed capacity assumptions.

## Implemented foundations

Literal retained-content search, bounded file extraction, local OCR/plate
candidates, configured transcription, current-evidence reports, conversation
imports/comparisons, descriptive writing profiles, and location comparisons now
exist for documented input profiles. Possible vehicle trips also appear from
current observations without an analysis run. These foundations are no longer a
future delivery sequence; [case analysis](analysis.md) and its focused guides own
current capabilities, limits, and configuration.

## Next: connect evidence and synthesize findings

The next milestone connects distinct records through inspectable source-backed
leads. Deliver useful bounded increments:

1. **Connect explicit values and events.** Start with reviewable exact-identifier
   matches and explicit links across distinct records. Show an anchor on both
   sides, identify copied material, and explain the reason for each link. Shared
   values remain leads, not verified identity or ownership. A later unified
   timeline should distinguish recorded times, extracted dates, plans, reported
   events, and upload time.
2. **Synthesize selected evidence.** Generate cited insights from an explicit
   selection, including supporting/conflicting passages, unknowns, and abstention
   when sources cannot support a claim. Use the configured private runtime by
   default; external inference needs separate approval through Cloudflare AI
   Gateway. Keep suggestions reviewable and linked to retained source anchors.
3. **Retrieve related passages.** Add case-scoped semantic retrieval alongside
   literal search when evaluation demonstrates useful results. Preserve index
   provenance and source selection, check access at query/result time, and test
   irrelevant as well as relevant hits.

**Complete when:** investigators can inspect every candidate link or summary
claim against its sources and reject suggestions without changing originals.
Unavailable evidence cannot leak through retrieval or generated answers. A graph
visualization is optional; dependable source navigation is required.

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

## Completion criteria for every release

Follow [AGENTS.md](../AGENTS.md) for case isolation, immutable originals,
provenance, uncertainty, and inference boundaries. Owners/editors initiate
processing and review; viewers inspect authorized results. Recheck current
access before execution, external submission, retries, and publication. Already
submitted external requests cannot be recalled.

Use the [validation workflow](validation.md), with synthetic/sanitized fixtures,
relevant permission/failure cases, mocked inference, and migration/Compose checks
where affected. Visible changes need the [design review](../DESIGN.md#completion-and-design-review)
and every scoped implementation needs the required simplicity review.

Feature-specific acceptance should establish that investigators can find the
source, understand coverage and uncertainty, and recover from failure. Planning
scores alone establish neither method suitability nor accuracy.
