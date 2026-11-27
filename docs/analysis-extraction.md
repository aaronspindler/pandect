# Document and media extraction

Derived results remain separate from originals. Select a document/image/audio
source or specific retained Phone call/FaceTime audio addition. Metadata-only
calls have nothing to decode/transcribe; supplied transcripts remain independent.

## Supported input profiles

| Operation | Supported input | Output and important limits |
| --- | --- | --- |
| Extract document text | UTF-8 with optional BOM, individual `.eml` files, or unencrypted PDF | Line/page anchors. UTF-8 BOM removal is recorded. PDF pages with usable text retain native text. Pages without native text are flagged for the separate approved OCR operation. Office formats are unsupported. |
| Image metadata | JPEG and single-frame PNG | Decoded dimensions, raw EXIF/XMP, orientation, device/software claims, capture-time and GPS candidates. An absent timezone is left absent. Raw values stay available alongside interpretations. Multi-picture JPEGs inspect only the primary image with explicitly partial coverage. |
| Audio metadata | [Supported WAV or AAC-in-M4A recording](original-previews.md#recordings) | Decoded duration, sample rate, channels and codec. Includes numerical RMS and one-second digital-zero/full-scale sample observations. Supplied values remain separate, with discrepancies disclosed. |
| Read text in images | JPEG/PNG source or scanned PDF page, printed English | Candidate transcription with an exact source or one-based page reference. The model can miss or misread characters. |
| Find vehicle plates | One retained JPEG/PNG image | Geometric detection and a model reading of a prepared crop for each possible plate. Crops, source rectangles, preparation details and detection scores remain reviewable. This is a bounded heuristic for roughly horizontal rectangular plates with a central Latin text line; other shapes, scripts and layouts can be missed. |
| Transcription | [Supported WAV or AAC-in-M4A recording](original-previews.md#recordings), English input | Candidate text with source recording intervals and original-timeline chunk offsets. No speaker identity or diarization. |

Uploads queue applicable deterministic file checks and case-approved model
checks. Follow-up analysis and hosted search use the exact saved extraction.
Verify uncertain material against the original.

## Email extraction

Upload one `.eml` file as a Document. **Extract document text** reads selected
message headers (From, To, Cc, Bcc, Date, Subject and Message-ID) and one body
locally. The displayed headers are decoded and unfolded. The body prefers plain
text, falling back to text extracted from HTML. HTML layout, scripts, embedded
resources, links in attributes and other non-text content are excluded. No URL is
fetched. The retained result and reader disclose when alternate bodies,
attachments or other MIME parts were skipped, when conversion was lossy, and when
decoding problems were detected. Consider them when assessing search coverage.

Search and follow-up analysis cite the retained extraction and the original email
file. Headers are claims in the email and can be missing or forged. They do not
create case people, establish sent time, or verify the sender. Downloading the
original preserves exact bytes, including attachments. Add attachments separately
for analysis. Mailboxes (`.mbox`, `.pst`) and Outlook `.msg` are unsupported.

The isolated parser bounds originals to 16 MiB, top-level headers to 64 KiB,
MIME structure to 128 parts and 12 levels, and decoded text to 2 MiB. The usual
case settings govern any later AI analysis of the retained text.

## Source anchors and interpretation

### Visible structure in images

Older OCR results with saved layout retain their word spans, reading orders, confidence scores and private previews. The current vision method returns candidate text anchored to the original image or PDF page.

### Vehicle plate candidates

A bounded OpenCV step detects up to five roughly rectangular regions. It scores geometry and the alignment of character-like connected components, then rectifies and prepares a central text crop. The approved vision model receives the prepared JPEG. Each reading records that JPEG's checksum, the original checksum and the normalized original rectangle. The private crop and source preview are available within the artifact budget. A weak geometric candidate retains its reading for review and contributes no searchable text. Review every candidate against the original.

### Other extraction anchors

PDF findings open the original at the cited one-based page. Native content-stream
order may differ from visual order. When a page already has usable native text,
extraction preserves it and does not search separately for text inside images on
that page. A separate approved OCR run renders only scanned pages, recording the PDFium version, source-page anchor, submitted JPEG checksum and model. The original remains in case storage.

Image recognition prepares a JPEG up to 2,048 pixels on its longest side. Preparation strips EXIF/XMP and composites transparency on white. A check can read at most 16 images or scanned pages, and records omitted sources or pages as coverage gaps.

EXIF/XMP are unverified claims. Times without offsets never acquire UTC; original
and modified times stay separate. Invalid GPS remains raw without coordinates.
Metadata/geotags or agreement with supplied fields prove neither authenticity
nor presence. Large/deep values show labelled prefixes/counts/omitted-depth markers;
bounded views never replace original bytes.

Transcription sends self-contained WAV chunks up to 30 seconds, validating segment
times against chunks and offsetting them onto the original timeline. Chunk-only
text has labelled coarse intervals/partial coverage; timed-segment gaps remain
unresolved/partial. Nonzero signals yielding empty text remain unresolved.
All-zero PCM chunks are not sent. Zero/near-full-scale observations diagnose no
silence, editing, dropout, noise, speech or event. Reported call duration never
substitutes for recording duration.

## Resource and privacy bounds

Private-storage reads allow 16 MiB or the configured lower limit, verifying size
and file-byte SHA-256. Call audio uses its **audio-byte** checksum; call-record and
attachment-manifest hashes have different domains.

Fresh isolated-mode Python parsers receive stdin bytes without case records,
paths, Django settings or provider secrets. stderr is discarded; safe codes replace
exceptions. Workers stop and reap parser process groups after 20
seconds. CPU allows 15 seconds; core dumps are disabled and descriptors bounded.
Output uses a private, file-size-limited temporary file, read to 8 MiB plus one
byte and closed on success/failure. Linux enforces 1 GiB address space; macOS
samples group RSS and terminates above 1 GiB, without a hard instantaneous ceiling.

Format limits: 100 PDF pages, 4 MiB/decompressed stream, 16 MiB accumulated streams,
2 MiB derived text, 50 million image pixels, 256 KiB image metadata and 30 minutes
of decoded audio. Decompression warnings fail closed. Unsupported encodings, encrypted
PDFs, malformed/truncated files and limits produce visible outcomes/recoverable
failures, never replaced originals or incomplete publication labelled complete.

PDF OCR renders one page at a time. OCR sends prepared images, and transcription
sends bounded recording chunks, through the
[approved AI Gateway route](analysis.md#external-ai-configuration). The client
checks access, source eligibility, approval and pinned execution before each
request. The worker repeats those checks before publication.

Primary references:

- [pypdf extraction/content-stream limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
- [Pillow metadata](https://pillow.readthedocs.io/en/stable/reference/Image.html) and [parser security](https://pillow.readthedocs.io/en/stable/handbook/security.html).
- [Workers AI Whisper profile](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/).
