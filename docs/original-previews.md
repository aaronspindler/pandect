# Original document and recording inspection

The Evidence reader provides private local previews for supported originals and
retained call recordings. Viewing a preview does not create an analysis run,
change an original, or require inference credentials. Original downloads remain
available for unsupported formats and preview failures.

## Documents

UTF-8 documents appear as escaped, read-only text. The display preserves decoded
characters, whitespace, and line endings; an initial UTF-8 byte-order mark is
omitted and disclosed. HTML-like text is displayed literally, never executed.
Download original returns the exact uploaded bytes.

PDFs use local page images with Previous page and Next page navigation, including
without JavaScript. Rendering uses the pinned `pypdfium2==5.15.2` dependency and
its bundled PDFium, in the existing isolated parser child process. The application
never embeds the uploaded PDF as executable application content. No previews are
stored and no remote document service is contacted.

Document previews accept at most 16 MiB of original bytes, at most 2 MiB of
decoded UTF-8 text, and PDFs with 1–100 pages. One requested PDF page is rendered
per image request, at no more than 2048 pixels per side and 4,194,304 pixels total.
The existing parser bounds apply: 20-second wall deadline, 15-second CPU limit,
1 GiB process memory bound, and 8 MiB output bound. Encrypted PDFs, malformed
documents, invalid page requests, and exhausted limits receive a useful fallback
while retaining Download original.

## Recordings

Standalone audio evidence and retained call audio have native playback controls.
No previous analysis run is needed. Supported recordings are intact, uncompressed
signed 16-bit PCM WAV, mono or stereo, with sample rates from 8–48 kHz, positive
duration up to 30 minutes, and file size up to 16 MiB. Other formats retain their
download action; playback does not transcode files.

Playback supports a single HTTP byte range, including open-ended and suffix
ranges, for browser seeking. Invalid or multiple ranges return `416`. The
recording is fully validated within the bounded parser before bytes are returned.
Each new range request rechecks case access and retained byte integrity.

## Privacy and validation

Every preview/playback route requires a verified, currently authorized case
member. Viewers may inspect originals, including archived evidence. Call playback
also checks the exact case and parent call of the attachment. Requests verify
file size and SHA-256 before decoding, use private/no-store responses and
`nosniff`, and never expose storage paths.

Run the focused PostgreSQL integration and parser checks with:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings document.tests.test_previews audio.tests.test_previews document.tests.test_analysis audio.tests.test_analysis core.analysis.tests.test_parser_dispatch --noinput
```

Synthetic fixtures cover access revocation, cross-case and foreign-attachment
requests, unchanged original downloads, UTF-8 escaping, real PDF rendering,
encrypted/malformed files, page limits, recording integrity, and byte-range
seeking. These checks do not qualify arbitrary document fidelity or audio content.

Renderer references: [Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html),
[package and bundled-library licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing).
