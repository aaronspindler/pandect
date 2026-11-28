# Original image, document, and recording previews

The Evidence reader offers private local previews of supported originals and
call recordings. Previews need no analysis run or inference credentials.

**Download original** opens the stored file without recomputing its checksum.
It returns an unavailable page if storage cannot open the file. When a preview
reports an integrity problem, ask the administrator to check the retained
original and storage before relying on a downloaded copy.

## Images

**Case Tools → Images** opens a paginated gallery of private image previews.
Select a title or thumbnail to open the evidence reader. The parser supports
JPEG and single-frame PNG up to 16 MiB and 50 million pixels, including 48 MP
iPhone RAW exports saved as JPEG within those limits. For multi-picture JPEGs
it previews the primary image. The original retains the other pictures.

The parser applies EXIF orientation, flattens transparency onto white, and
encodes a JPEG preview up to 2048 pixels per side without source metadata.
It leaves the original and checksum intact. Previews work for archived images
and with JavaScript disabled. JavaScript adds loading and failure messages.
Unsupported, corrupt, oversized, missing, or changed files receive the same
image-preview failure message. A retained crop or marked region can appear
beside its finding for comparison.

## Documents

UTF-8 documents display escaped read-only text, preserving decoded characters,
whitespace, and line endings. The reader discloses when it omits an initial BOM.
It displays HTML-like text literally. Downloads serve the available stored bytes.

A `.eml` Document shows decoded headers and body text. The preview prefers the
plain-text body and extracts literal text from an HTML-only body. The reader
escapes the result and excludes attachments, links embedded as attributes,
and remote content. **Download original** preserves the entire email file.
See [email extraction](analysis-extraction.md#email-extraction) for coverage.

PDFs render local page images with Previous/Next navigation and a **Page number**
field with **Go to page**, including without JavaScript. Page changes retain the
evidence-list filters. Invalid page requests offer **Open first page**. PDFium
renders in the isolated parser. The reader displays the rendered page image.
Preview images stay local and are generated per request.

Document previews allow up to 16 MiB per original, 2 MiB decoded text and
100 PDF pages. Each PDF image request renders one page at up to 2,048 pixels
per side. See [parser bounds](analysis-extraction.md#resource-and-privacy-bounds).
Encrypted or malformed PDFs, invalid pages and exhausted limits offer
**Download original**. Missing files or integrity failures ask for an
administrator's storage check.

## Recordings

Audio evidence and call recordings offer native playback without prior analysis.
When a call attachment has audio and a transcript, its player and audio download
appear before the transcript.
Supported inputs for playback, metadata and transcription are intact,
uncompressed signed 16-bit little-endian PCM WAV or AAC in M4A with one audio
stream and no video. Both support mono or stereo at 8–48 kHz. Duration must be
positive and at most 30 minutes. Original bytes and decoded signed 16-bit PCM
are each bounded to 16 MiB. Playback serves the validated original without
transcoding. The player explains unsupported formats and flags missing or
checksum-changed recordings.

Seeking supports one HTTP byte range, including open-ended/suffix ranges.
Invalid/multiple ranges return `416`. The bounded parser validates the entire
recording before returning bytes. Each range request rechecks access and integrity.
M4A decoding uses PyAV and its wheel's FFmpeg libraries in the isolated parser.
Metadata and transcription use decoded PCM while retaining the original-byte
checksum and source timeline. M4A timestamps must be continuous from zero;
ambiguous timelines remain unsupported.

## Privacy and validation

Every route requires a verified, currently authorized case member. Viewers may
inspect archived originals. Call playback checks the attachment's exact case and
parent. Preview and playback requests verify file size and SHA-256 before decoding.
Responses use private/no-store and `nosniff` and omit storage paths.

Run focused PostgreSQL integration/parser checks:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.tests.settings document.tests.test_previews audio.tests.test_previews document.tests.test_analysis audio.tests.test_analysis audio.tests.test_m4a core.analysis.tests.test_parser_dispatch --noinput
```

Synthetic fixtures cover revocation, cross-case/foreign attachments, unchanged
downloads, UTF-8 escaping, real PDF rendering, encryption/malformed files, page
limits, recording integrity, and seeking. They do not qualify arbitrary document
fidelity or audio content.

Renderer references: [Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html),
[package and bundled-library licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing),
[PyAV installation and bundled FFmpeg](https://pyav.basswood.io/docs/stable/overview/installation.html).
