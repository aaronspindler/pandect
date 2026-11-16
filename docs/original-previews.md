# Original document and recording inspection

The Evidence reader offers private local previews of supported originals/call
recordings without analysis runs, original changes, or inference credentials.
Unsupported formats and preview failures retain original downloads.

## Documents

UTF-8 documents display escaped read-only text, preserving decoded characters,
whitespace, and line endings. An initial BOM is omitted and disclosed. HTML-like
text is never executed; downloads preserve exact uploaded bytes.

PDFs render local page images with Previous/Next navigation, including without
JavaScript. Pinned `pypdfium2==5.15.2` and bundled PDFium run in the isolated parser
child process. Uploaded PDFs are never embedded as executable app content;
previews are neither stored nor sent to remote services.

Limits: 16 MiB original, 2 MiB decoded text, PDFs of 1–100 pages. Each image request
renders one page at up to 2048 pixels per side/4,194,304 total. Parser bounds are
20 seconds wall time, 15 seconds CPU, 1 GiB memory, and 8 MiB output. Encrypted or
malformed PDFs, invalid pages, and exhausted limits show a fallback with Download original.

## Recordings

Audio evidence/call recordings offer native playback without prior analysis.
Supported WAVs are intact, uncompressed signed 16-bit PCM, mono/stereo, 8–48 kHz,
with positive duration up to 30 minutes and size up to 16 MiB. Other formats keep
downloads; playback does not transcode.

Seeking supports one HTTP byte range, including open-ended/suffix ranges.
Invalid/multiple ranges return `416`. The bounded parser validates the entire
recording before returning bytes. Each range request rechecks access and integrity.

## Privacy and validation

Every route requires a verified, currently authorized case member; viewers may
inspect archived originals. Call playback checks the attachment's exact case and
parent. Requests verify file size/SHA-256 before decoding and return private/no-store
responses with `nosniff`, without storage paths.

Run focused PostgreSQL integration/parser checks:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings document.tests.test_previews audio.tests.test_previews document.tests.test_analysis audio.tests.test_analysis core.analysis.tests.test_parser_dispatch --noinput
```

Synthetic fixtures cover revocation, cross-case/foreign attachments, unchanged
downloads, UTF-8 escaping, real PDF rendering, encryption/malformed files, page
limits, recording integrity, and seeking. They do not qualify arbitrary document
fidelity or audio content.

Renderer references: [Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html),
[package and bundled-library licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing).
