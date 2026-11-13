# Interface design references

## Brand artwork

The [selected Pandect arch identity](brand/pandect-arch/README.md) includes
transparent full-logo and symbol PNGs, light/dark/monochrome previews, and
generation provenance. The shared header and case sidebar use optimized light
and dark logo variants from `ui/static/ui/brand/`; browser and touch icons use
the standalone arch.

## Interface references

- [Quiet workspace](quiet-evidence-reference.png): selected on 2026-11-13 for the
  evidence rebuild. It emphasizes the original and source-linked findings, a
  compact rail, one Info inspector, and a More menu. The implementation shows real
  processing output rather than reproducing illustrative findings.

The planning reference images were generated with the built-in Image
Generation tool on 2026-10-25. They are visual references, not application assets
or evidence of a running interface. No private investigation material was used.

- [Selected option 3](evidence-workspace-reference.png): initial three-pane
  workspace with four visible records.
- [Density refinement](evidence-workspace-dense.png): compact toolbar, many more
  visible records, concise provenance, read-only description, and disclosed
  integrity details. The partial next row indicates a scrollable collection;
  implementation row-count targets are specified separately and must be measured
  in the rendered application.

The [screen specification](../interface-design.md) takes precedence over
incidental mockup copy or pixel measurements.

## Retained implementation captures

These earlier browser captures use a synthetic case. They document a past
implementation, not the current layout or a new validation run:

- [Desktop](implementation-desktop.png), at 1487 × 1058 CSS pixels.
- [Mobile](implementation-mobile.png), at 390 × 844 CSS pixels.
- [Forced colors](implementation-forced-colors.png), at 1440 × 1024 CSS pixels.
- [Full reference comparison](comparison-desktop.png) and
  [focused evidence list comparison](comparison-evidence-list.png).

See [browser validation](../validation.md#browser-checks) for density checks,
test coverage, and validation limits. Additional synthetic browser-test captures
are written to ignored `data/validation/browser/`.
