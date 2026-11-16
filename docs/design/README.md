# Interface design references

## Brand artwork

The [approved arch identity](brand/pandect-arch/README.md) includes transparent
logos/symbols, light/dark/monochrome previews, and generation provenance. The
header and case sidebar use optimized variants from `ui/static/ui/brand/`;
browser and touch icons use the standalone arch.

## Interface references

- [Quiet workspace](quiet-evidence-reference.png): selected on 2026-11-13 for the
  evidence rebuild, emphasizing originals, source-linked findings, a compact
  rail, Info inspector, and More menu. Implementation uses actual processing
  output rather than illustrative findings.

The following planning images were generated with the built-in Image Generation
tool on 2026-10-25, without private investigation material. They are references,
not application assets or evidence of a running interface:

- [Selected option 3](evidence-workspace-reference.png): initial three-pane
  workspace with four visible records.
- [Density refinement](evidence-workspace-dense.png): compact toolbar, more records,
  concise provenance, read-only description, and disclosed integrity details.
  A partial next row suggests scrolling; measure implementation row targets in
  the rendered application.

The [screen specification](../interface-design.md) takes precedence over mockup
copy and pixel measurements.

## Retained implementation captures

These synthetic-case captures record an earlier implementation, not the current
layout or a new validation run:

- [Desktop](implementation-desktop.png): 1487 × 1058 CSS pixels.
- [Mobile](implementation-mobile.png): 390 × 844 CSS pixels.
- [Forced colors](implementation-forced-colors.png): 1440 × 1024 CSS pixels.
- [Full comparison](comparison-desktop.png) and
  [evidence list comparison](comparison-evidence-list.png).

See [browser validation](../validation.md#browser-checks) for density checks,
capture locations, and limits. The [quiet workspace QA report](../archive/quiet-evidence-qa.md)
is historical; keep new captures in the documented ignored locations.
