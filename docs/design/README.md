# Interface design references

## Brand artwork

The [selected Pandect arch identity](brand/pandect-arch/README.md) includes
transparent full-logo and symbol PNGs, light/dark/monochrome previews, and
generation provenance. The shared header and case sidebar use optimized light
and dark logo variants from `static/pandect/brand/`; browser and touch icons use
the standalone arch.

## Interface references

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

## Implemented interface captures

The following are real browser captures of the Django implementation using a
synthetic case, not generated mockups:

- [Desktop](implementation-desktop.png), at 1487 × 1058 CSS pixels.
- [Mobile](implementation-mobile.png), at 390 × 844 CSS pixels.
- [Forced colors](implementation-forced-colors.png), at 1440 × 1024 CSS pixels.
- [Full reference comparison](comparison-desktop.png) and
  [focused evidence list comparison](comparison-evidence-list.png).

See [browser validation](../validation.md#browser-checks) for coverage,
density targets, and the location of current synthetic captures.

## Final refinement prompt

The final image edits the first density revision of selected option 3 using this
prompt:

> Refine the attached Pandect UI mockup, preserving its existing visual style and
> three-pane arrangement. Output ONE desktop web-app screenshot target1440x1024.
> Fix ONLY these visible layout issues: 1) The evidence list's last row 'Source
> email' is partly overlapped by the pagination. Make FOURTEEN complete, readable
> rows fit between the list toolbar and footer, with no overlap/clipping. Compact
> the toolbar to156px total: heading/Add56px, search44px, filter44px plus12px
> spacing. Evidence rows56px minimum,16px title14px metadata,44px or larger hit
> area. Pagination footer44px in its own space. Preserve all14titles; no tiny
> fonts. 2) The selected text record metadata should say 'Text' rather than 'Text
> document'. 3) Investigator description should be normal read-only prose, not an
> editable bordered text field; remove the duplicate lower Edit details button,
> keeping the top Edit details button. 4) The footer range is '1–25 of 38',
> because25 records are loaded on this page and14 fit in the viewport; show a
> subtle scroll indicator so additional rows are discoverable. Everything else
> remains the same: sidebar, readable original text, compact source strip,
> collapsed Integrity details, no delete action. Remove any accidental overlaps,
> preserve clear font rendering and contrast. All dates on/before25Oct2026 and
> synthetic content. Output screen only.
