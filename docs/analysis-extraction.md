# Document and media extraction

The case Analysis workflow retains derived findings separately from originals.
Select a document/image/audio source, or the particular retained audio addition
on a Phone call or FaceTime record. A metadata-only call has no recording to
decode or transcribe. Supplied transcripts remain independent retained material.

## Supported input profiles

| Operation | Supported input | Output and important limits |
| --- | --- | --- |
| Native text | UTF-8, with optional BOM; unencrypted native-text PDF | Exact derived text with line/page anchors. UTF-8 BOM removal is recorded. No native text on a PDF page means visual review is needed; it does not establish a blank page. Mixed text/scanned pages produce partial coverage. Office formats are unsupported. |
| Image metadata | Single-frame JPEG and PNG | Decoded dimensions, raw EXIF/XMP, orientation, device/software claims, capture-time and GPS candidates. An absent timezone is left absent. Raw values stay available alongside interpretations. |
| Audio metadata | Uncompressed signed 16-bit little-endian PCM WAV, mono/stereo, 8–48 kHz | Decoded duration, sample rate, channels and codec; numerical RMS and one-second digital-zero/full-scale sample observations. Supplied values remain separate, with discrepancies disclosed. |
| Printed OCR | JPEG/PNG source or separately retained page image, printed English profile | Candidate text with normalized rectangles, reading order, uncertainty and alternative readings. Handwriting, other scripts, PDF rendering and complex layouts are outside the supported profile. |
| Transcription | The supported WAV profile, English input | Candidate text with source recording intervals and original-timeline chunk offsets. No speaker identity or diarization. Non-English input profiles are unsupported. |

The fixture suite checks decoding, supported/unsupported states, boundaries and
inference-response handling using synthetic material and mocked Gateway replies.
It does not establish a real provider's recognition accuracy. OCR and transcription
remain model suggestions that investigators must inspect against the originals;
names, accents, overlap, blur and difficult layout can produce errors.

## Source anchors and interpretation

Native PDF text uses one-based page numbers. The extraction order follows the
native PDF content stream; it may disagree with visual reading order or hidden
text. Open/download the original to compare the cited page. Native extraction
does not render PDF pages or execute embedded content. Scanned pages need a
separately retained JPEG/PNG page image for this OCR input profile.

OCR prepares a JPEG of at most 2048 pixels on its longest side, with EXIF and XMP
omitted from submission. Transparency is composited on white. Normalized regions
refer to the original image's
**unrotated pixel coordinate space**. The preparation resizes pixels without
applying EXIF orientation or cropping. The original dimensions and preparation
version remain in the finding. A model-reported rotation is a candidate about
text direction, not a rewritten original. Unreadable/empty/unsupported findings
are distinct from successfully extracted printed text.

Region inspection reuses that exact prepared JPEG as a private derived preview,
so the normalized rectangles align with the submitted pixels. A run retains at
most 2 MiB of decoded JPEG preview bytes. Further previews are omitted with an
explicit warning; this display limit does not change the OCR text or its coverage
outcome. The retained original remains available for inspecting those regions.

EXIF times and XMP remain unverified source claims. A local capture-time candidate
without an EXIF offset is never silently assigned UTC. Original-time and
modification-time fields retain their separate values. Invalid GPS components
remain raw metadata with no resolved coordinate. Metadata presence, a geotag,
or agreement with an investigator-supplied field does not prove authenticity or
presence.

Large individual metadata values and deeply nested structures use explicitly
labelled prefixes/counts or omitted-depth markers. The complete original file
remains retained; a bounded metadata view is not a replacement for its bytes.

Transcription sends self-contained WAV chunks of at most 30 seconds. A segment's
times are validated against its chunk and offset onto the original recording
timeline. When only chunk text is returned, its coarse interval is labelled and
the outcome is partial. Gaps between timed segments remain explicit unresolved
intervals and produce partial coverage. Empty text for a nonzero signal is
unresolved. Chunks
with exclusively zero-valued PCM samples are not sent for inference. Zero-valued
or near-full-scale samples are numerical observations, not diagnoses of silence,
editing, dropouts, noise, speech, or events. A call's reported duration is not
silently treated as the duration of its retained recording.

## Resource and privacy bounds

Files are read through private storage with a 16 MiB maximum, or a smaller
configured analysis limit. Actual bytes and size must match the original
file-byte SHA-256. Retained call audio uses its **audio-byte** checksum; the
call record and attachment-manifest hashes describe other domains.

Parsers run in fresh isolated-mode Python subprocesses. The child receives bytes
through stdin and no case records, storage paths, Django configuration or provider
credentials. stderr is discarded and safe error codes replace exception strings.
The worker kills/reaps a child after 20 seconds; child CPU is limited to 15 seconds,
core dumps are disabled, and output and open descriptors are bounded. Linux
workers enforce a 1 GiB address-space limit. macOS does not support the same
address-space limit here; the development runner samples child RSS and terminates
it above 1 GiB. That sampled check is not a hard instantaneous memory limit.

Additional format limits apply: at most 100 PDF pages, 4 MiB per decompressed
content stream and 16 MiB accumulated page streams, 2 MiB derived text, 25 million
image pixels, 256 KiB extracted image metadata, and 30 minutes of decoded WAV.
Decompression warnings fail closed. Unsupported encodings, encrypted PDFs,
malformed/truncated sources and limits remain visible outcomes or recoverable
failures; they never replace originals or publish an incomplete run as complete.

These subprocess limits provide resource isolation, not an OS security sandbox.
Keep the deployed worker unprivileged and confined, with private storage and
restricted network access. All ML/LLM inference uses the central authenticated
Cloudflare AI Gateway path with current case access checks immediately before
and after requests. No direct provider endpoint is called by these extractors.
The existing job/result workflow handles retries, revocation and atomic
publication; each extraction retains its parser/model and source versions.

Primary references:

- [pypdf text extraction and content-stream memory limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
- [Pillow image metadata APIs](https://pillow.readthedocs.io/en/stable/reference/Image.html) and [parser security considerations](https://pillow.readthedocs.io/en/stable/handbook/security.html).
- [Cloudflare Workers AI Whisper input/output profile](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/).
- [Cloudflare Workers AI Llama 4 Scout vision profile](https://developers.cloudflare.com/workers-ai/models/llama-4-scout-17b-16e-instruct/).
