# Leaflet 1.9.4

Distribution files from <https://unpkg.com/leaflet@1.9.4/>, served
locally. BSD-2-Clause license in `LICENSE`. Release and published integrity
hashes: <https://leafletjs.com/download.html>.

SHA-256:

- `leaflet.js`: `dc71f8a6880bc3ca1bd9fa8dc5f1af48c702dc510b0a78240a07c5feed7ce935`
- `leaflet.css`: `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6`

The case map uses DOM markers. All upstream images are included so the original
stylesheet also works with manifest-based static-file collection. No CDN calls
are made at runtime.

The JavaScript source-map comment is removed because the map is not vendored.
Runtime code is unchanged; removing the dangling reference allows manifest static
collection to validate all referenced assets.
