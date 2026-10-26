# Pandect arch identity

Selected by the user on 2026-10-26: **A — arch + manuscript**. The symbol combines a gothic pointed arch, an open-page base, three connected evidence points, and a diamond terminal. The wordmark reads **Pandect**.

## Artwork

| Use | Full logo | Standalone symbol |
| --- | --- | --- |
| Light backgrounds | [Oxblood PNG](pandect-logo-oxblood.png) | [Oxblood PNG](pandect-symbol-oxblood.png) |
| Dark backgrounds | [Ivory PNG](pandect-logo-ivory.png) | [Ivory PNG](pandect-symbol-ivory.png) |
| Single-color reproduction | [Black PNG](pandect-logo-black.png) | [Black PNG](pandect-symbol-black.png) |

All six files are transparent PNGs. Full logos are 2048 pixels wide; symbols are 1024 pixels high. Preserve their aspect ratio and clear space. These are raster assets; editable vector paths and a font file are not included.

## Preview and use

Open [preview.html](preview.html) for light, dark, monochrome, and actual-size symbol comparisons. [Overview PNG](preview-overview.png) and individual preview PNGs are provided for sharing.

Oxblood is `#692D39`, ivory/parchment is `#F1E9DA`, and charcoal is `#1F1C20`. Use oxblood on light backgrounds and ivory on dark backgrounds. Use the symbol alone where the full blackletter name would be too small to read. The 24, 32, 48, and 64 pixel proofs support size review; the smallest details need more space than the outer silhouette.

Visual review: the arch remains recognizable at 24 pixels, but the nodes and
diamond lose definition. Prefer at least 32 pixels for the standalone symbol,
and 48 pixels when the internal connection details need to read clearly.

Treat the image as decorative when adjacent accessible text already names Pandect; otherwise use an accessible name of Pandect.

## Website integration

The shared application header and case sidebar render optimized 432-pixel-wide
logo variants from `static/pandect/brand/`, with the ivory image selected by the
browser's dark-mode preference. The linked artwork has the accessible name
Pandect; forced-colors mode displays readable system-font text. Browser and
Apple touch icons use the arch symbol on ivory at 32 and 180 pixels respectively.
These web files are proportionally resized derivatives of the final PNGs above.
Their source relationships and hashes are recorded in `provenance.json`.

Local integration validation on 2026-10-26 passed `make check` (190 application
tests), six browser checks covering Chromium and WebKit, and the isolated Compose
smoke workflow. Logo review covered desktop/tablet/mobile layouts down to 320
pixels, light/dark images, keyboard activation and focus, 200% text sizing, and
Chromium forced-colors fallback. The production image served all four optimized
brand files through WhiteNoise with the expected PNG type and file hashes.

## Provenance and verification

Built-in Image Generation produced the approved artwork and faithful transparent edits. Deterministic browser rendering preserves the generated alpha masks while normalizing clear space, output dimensions, and exact solid ink colors. Original generation files and the approved reference are retained under [source/](source/).

[provenance.json](provenance.json) records the production briefs, source hashes, palette, dimensions, and checks. [manifest.json](manifest.json) records file hashes. Transparent backgrounds, solid fill colors, loaded preview images, and mobile preview reflow were checked.
