# Document and media extraction

Derived results remain separate from originals. Select a document/image/audio
source or specific retained Phone call/FaceTime audio addition. Metadata-only
calls have nothing to decode/transcribe; supplied transcripts remain independent.

## Supported input profiles

| Operation | Supported input | Output and important limits |
| --- | --- | --- |
| Native text | UTF-8, with optional BOM; unencrypted native-text PDF | Exact derived text with line/page anchors. UTF-8 BOM removal is recorded. No native text on a PDF page means visual review is needed; it does not establish a blank page. Mixed text/scanned pages produce partial coverage. Office formats are unsupported. |
| Image metadata | JPEG and single-frame PNG | Decoded dimensions, raw EXIF/XMP, orientation, device/software claims, capture-time and GPS candidates. An absent timezone is left absent. Raw values stay available alongside interpretations. Multi-picture JPEGs inspect only the primary image with explicitly partial coverage. |
| Audio metadata | Uncompressed signed 16-bit little-endian PCM WAV, mono/stereo, 8–48 kHz | Decoded duration, sample rate, channels and codec; numerical RMS and one-second digital-zero/full-scale sample observations. Supplied values remain separate, with discrepancies disclosed. |
| Local OCR | JPEG/PNG source or separately retained page image, printed English | Tesseract candidate text grouped by line, normalized rectangles, reading order, minimum word confidence and engine version. Handwriting, mixed text, other languages, direct PDF rendering and complex layouts are unsupported. |
| Find vehicle plates | One retained JPEG/PNG image | Local geometric region detection and three Tesseract readings per possible plate. Crops, source rectangles, processing settings and engine scores remain reviewable. This is a bounded heuristic for roughly horizontal rectangular plates with a central Latin text line; other shapes, scripts and layouts can be missed. |
| Transcription | The supported WAV profile, English input | Candidate text with source recording intervals and original-timeline chunk offsets. No speaker identity or diarization. Non-English input profiles are unsupported. |

Tesseract/English data runs entirely on the worker, without credentials, network
requests or remote fallback. Uploads and **Run checks again** queue image metadata,
OCR and plate search; successful ordinary OCR queues identifier/date/keyword
checks on that exact extraction. Document JPEG/PNG page images support manual
**Read text in images**.

`.jpg`/`.jpeg` support includes 48 MP iPhone RAW exports saved as JPEG within
byte/pixel limits. MPO previews/metadata/OCR inspect only the primary image,
explicitly reporting partial coverage; downloads preserve every embedded picture.
Animated PNG and native RAW decoding remain unsupported.

Only **Printed English** is offered; historical results stay readable. Metadata
version 3/OCR version 5 include larger JPEGs and primary-image handling. XMP
interpretation ignores/counts terminal NULs while retaining raw claims; malformed
XML or DTD/entity-bearing packets remain uninterpreted. OCR summaries separately
count recognized regions, coverage and unsupported inputs, explicitly reporting
no text, engine version and language. Queued old methods fail recoverably;
start a new run without rewriting parameters/history.

Tesseract scores are uncalibrated engine scores; each line keeps its minimum
word score, with scores below 80 producing partial coverage. English selection
is not language detection. High scores cannot rule out missed regions, handwriting
or scripts. No text is partial/unresolved, never proof of blankness. Check against
originals. Ordinary OCR can miss angled/small text, including plates; the separate
plate profile is below. Synthetic printed images test real recognition; engine
fixtures cover failures/publication, without establishing real-evidence accuracy.
Transcription defaults to private whisper.cpp; Cloudflare requires separate
selection/approval. Routine inference is mocked; see [qualification](local-analysis.md).
Transcript text stays searchable as extracted content and opens at its saved
source anchors. The findings catalogue surfaces timed segments flagged for
uncertain speech so an investigator can check the audio.

## Source anchors and interpretation

### Vehicle plate candidates

Upload/**Run checks again** includes plate search. To run alone, choose **Find
vehicle plates** under **Additional analysis**, selecting one original per run.
Originals, vehicle records and earlier results remain unchanged.

Version 2 uses headless OpenCV on a metadata-free preview up to 2048 pixels on
its longest side, finding convex roughly horizontal quadrilaterals. It inspects
1,000 contours at most, suppresses duplicate outlines, retains up to five regions and
discloses reached limits. Geometric candidates are untrained: signs/windows can
match; detecting nothing does not establish that no plate is present.

Regions map back to original unrotated pixels and are cropped at original
resolution, perspective-corrected within 1024 × 512 pixels. Three variants read
the central 22–80% vertical band: 2× full width, then 2×/3× with 2% horizontal
insets. Grayscale autocontrast, Lanczos resizing and a 20-pixel white border
precede quality-95 JPEG encoding. Before borders, input is capped at 1200 × 400.
English single-line Tesseract (`--psm 7`, word dictionaries disabled) makes at
most 15 calls within parser CPU/memory/output/time limits.

**Plate-likeness** is separate from OCR scoring. The fixed `plate-likeness.v1`
0–100 heuristic awards up to 20 points for rectangularity/aspect, 60 for character
rows, ten for readability and ten for variant agreement. Character-row analysis
removes the first recognition JPEG's border, uses both Otsu polarities, component
size/coverage and median baseline deviation, requiring 3–12 components and bounded
foreground density. Agreement ignores ASCII case/punctuation without changing
retained readings.

This weighted ranking is neither a probability nor trained classification.
Regions below 50 are hidden; **Show weak regions** reveals readings, scores,
signals and private previews. Direct links reveal weak findings at their saved
numbers. Downloads retain every finding. Signs may pass, and blurred, obscured,
or unfamiliar plates may fail. No general accuracy is established.

Above-threshold readings with at least three ASCII letters/digits become searchable
candidates, preserving punctuation/conflicts. Weak OCR and shorter text remain
context, excluded from search, highlights and extraction inputs. Unscored historical
results keep their presentation; queued version 1 needs a new run. Outcomes are
always **Partial**, because detection cannot establish exhaustive coverage.
Candidates never update vehicles or establish identity, ownership, presence or
association.

Readings retain corners/rectangles, crop/resize settings, parser/detector/engine
versions, minimum word scores and original/recognition-JPEG hashes. Private crops
appear beside text; **Show region on source image** opens original-region previews
through authorized artifact routes and immutable history. Parser crop previews
allow 1 MiB within the cumulative 2 MiB retained-preview budget. Omissions retain
readings/provenance with disclosure. No character substitution, jurisdiction
guessing or automatic conflict resolution occurs. MPO searches only the primary.

### Other extraction anchors

PDF anchors use one-based pages and native content-stream order, which may differ
from visual/hidden-text order. Compare original pages. Extraction neither renders
pages nor executes embedded content; scanned pages need separately retained
JPEG/PNG images for OCR.

OCR prepares a JPEG up to 2048 pixels long, excluding EXIF/XMP and compositing
transparency on white. Normalized rectangles use original **unrotated pixels**;
resizing applies no orientation/cropping/rotation correction. Findings retain
original dimensions/preparation version and separate unsupported/unrecognized
input from text.

Region previews reuse that exact JPEG for alignment, retaining up to 2 MiB decoded
preview bytes per run. Omitted previews carry warnings without changing text or
coverage; originals remain inspectable.

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
exceptions. Workers kill/reap process groups, including OCR children, after 20
seconds. CPU allows 15 seconds; core dumps are disabled and descriptors bounded.
Output uses a private, file-size-limited temporary file, read to 8 MiB plus one
byte and closed on success/failure. Linux enforces 1 GiB address space; macOS
samples group RSS and terminates above 1 GiB, without a hard instantaneous ceiling.

Format limits: 100 PDF pages, 4 MiB/decompressed stream, 16 MiB accumulated streams,
2 MiB derived text, 50 million image pixels, 256 KiB image metadata and 30 decoded
WAV minutes. Decompression warnings fail closed. Unsupported encodings, encrypted
PDFs, malformed/truncated files and limits produce visible outcomes/recoverable
failures, never replaced originals or incomplete publication labelled complete.

Resource isolation is not an OS security sandbox. Confine unprivileged workers,
private storage and networking. Tesseract receives prepared JPEG stdin, never
paths/input lists, with 15-second calls, one thread, bounded private stdout and
discarded stderr. Parser environments exclude secrets. Missing engines/English
data yield safe recovery instructions, never remote fallback.

Other inference uses shared pinned private runtimes or separately selected/approved
authenticated Cloudflare AI Gateway, checking access immediately before/after
requests. Local failure never triggers external requests; extractors call no direct
public-provider endpoints. Shared runs handle recovery, revocation and atomic
publication, retaining parser/model/source versions.

Primary references:

- [pypdf extraction/content-stream limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
- [Pillow metadata](https://pillow.readthedocs.io/en/stable/reference/Image.html) and [parser security](https://pillow.readthedocs.io/en/stable/handbook/security.html).
- [Workers AI Whisper profile](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/).
- [Tesseract command line/TSV](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
- [Tesseract installation/data](https://tesseract-ocr.github.io/tessdoc/Installation.html).
