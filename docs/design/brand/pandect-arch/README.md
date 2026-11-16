# Pandect arch identity

Selected by the user on 2026-10-26: **A — arch + manuscript**. The symbol combines a gothic
pointed arch, open-page base, three connected evidence points, and diamond
terminal. The wordmark reads **Pandect**.

## Artwork

| Use | Full logo | Standalone symbol |
| --- | --- | --- |
| Light backgrounds | [Oxblood PNG](pandect-logo-oxblood.png) | [Oxblood PNG](pandect-symbol-oxblood.png) |
| Dark backgrounds | [Ivory PNG](pandect-logo-ivory.png) | [Ivory PNG](pandect-symbol-ivory.png) |
| Single-color reproduction | [Black PNG](pandect-logo-black.png) | [Black PNG](pandect-symbol-black.png) |

All six PNGs are transparent: logos are 2048 pixels wide; symbols are 1024 pixels
high. Preserve aspect ratio and clear space. Editable vectors and font files are
not included.

## Preview and use

[preview.html](preview.html) shows light, dark, monochrome, and actual-size symbol
comparisons. [Overview](preview-overview.png) and individual PNG previews are
available for sharing.

Use oxblood (`#692D39`) on light backgrounds and ivory/parchment (`#F1E9DA`) on
dark; charcoal is `#1F1C20`. Use the symbol when the full wordmark would be
unreadable. The 24/32/48/64-pixel proofs show the arch recognizable at 24 pixels,
but nodes and diamond lose definition. Prefer at least 32 pixels, or 48 when
connection details must read clearly.

Treat artwork as decorative beside accessible Pandect text; otherwise give it
the accessible name **Pandect**.

## Website integration

The header and case sidebar use proportionally resized 432-pixel-wide logos from
`ui/static/ui/brand/`, selecting ivory in dark mode. Linked artwork is named
Pandect; forced colors use system-font text. Browser and Apple touch icons use
the arch on ivory at 32 and 180 pixels. `provenance.json` records source
relationships and hashes.

## Provenance and verification

Built-in Image Generation produced the approved artwork and transparent edits.
Deterministic browser rendering retained the alpha masks while normalizing clear
space, dimensions, and solid ink colors. Originals remain in [source/](source/).

[provenance.json](provenance.json) records briefs, sources, palette, dimensions,
and checks; [manifest.json](manifest.json) records file hashes. Transparency,
solid colors, preview loading, and mobile reflow were checked.
