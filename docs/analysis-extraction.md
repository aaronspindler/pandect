# Document and media extraction

Analysis retains derived results separately from originals.
Select a document/image/audio source, or the particular retained audio addition
on a Phone call or FaceTime record. A metadata-only call has no recording to
decode or transcribe. Supplied transcripts remain independent retained material.

## Supported input profiles

| Operation | Supported input | Output and important limits |
| --- | --- | --- |
| Native text | UTF-8, with optional BOM; unencrypted native-text PDF | Exact derived text with line/page anchors. UTF-8 BOM removal is recorded. No native text on a PDF page means visual review is needed; it does not establish a blank page. Mixed text/scanned pages produce partial coverage. Office formats are unsupported. |
| Image metadata | JPEG and single-frame PNG | Decoded dimensions, raw EXIF/XMP, orientation, device/software claims, capture-time and GPS candidates. An absent timezone is left absent. Raw values stay available alongside interpretations. Multi-picture JPEGs inspect only the primary image with explicitly partial coverage. |
| Audio metadata | Uncompressed signed 16-bit little-endian PCM WAV, mono/stereo, 8–48 kHz | Decoded duration, sample rate, channels and codec; numerical RMS and one-second digital-zero/full-scale sample observations. Supplied values remain separate, with discrepancies disclosed. |
| Local OCR | JPEG/PNG source or separately retained page image, printed English | Tesseract candidate text grouped by line, normalized rectangles, reading order, minimum word confidence and engine version. Handwriting, mixed text, other languages, direct PDF rendering and complex layouts are unsupported. |
| Find vehicle plates | One retained JPEG/PNG image | Local geometric region detection and three Tesseract readings per possible plate. Crops, source rectangles, processing settings and engine scores remain reviewable. This is a bounded heuristic for roughly horizontal rectangular plates with a central Latin text line; other shapes, scripts and layouts can be missed. |
| Transcription | The supported WAV profile, English input | Candidate text with source recording intervals and original-timeline chunk offsets. No speaker identity or diarization. Non-English input profiles are unsupported. |

OCR uses the installed Tesseract engine and English language data entirely on the
worker host. It needs no AI credentials, makes no network request and never falls
back to external processing. Image uploads and **Run checks again** queue
OCR, image metadata, and plate search. Successful ordinary OCR then queues
identifier/date and keyword analysis against that exact retained extraction. JPEG/PNG page images stored as
documents can use **Read text in images** in manual analysis.

JPEG support includes `.jpg` and `.jpeg` files, including 48 MP iPhone RAW
exports saved as JPEG, within the byte and pixel limits below. For multi-picture
JPEGs (MPO), previews, metadata and OCR use only the primary image. Analysis
reports partial coverage and warns that additional embedded pictures were not
analyzed; the complete original remains available for download. Animated PNG
remains unsupported. Native RAW files are not decoded.

Only **Printed English** is offered; earlier results remain readable. Image
metadata method version 3 and OCR version 5 include the larger JPEG profile and
primary-image handling. Metadata interpretation
ignores terminal NUL bytes in XMP packets, records their count, and retains the
unaltered raw XMP claim. Malformed XML and DTD/entity-bearing packets remain
uninterpreted. OCR summaries count recognized text regions separately from
coverage and unsupported-input records; no recognized text is reported explicitly.
OCR retains the local engine version and language setting.
Queued older method versions fail with a recoverable version
message; start a new run rather than rewriting their retained parameters or history.

Tesseract scores are engine scores, not calibrated probabilities. A line retains
its minimum word score; scores below 80 mark coverage partial. The English setting
is not language detection. Missing regions, handwriting and other scripts can go
unrecognized even with high scores. No recognized text is partial/unresolved,
never proof that the image is blank. Review candidate text against the original.
Small or angled lettering in photographs, including vehicle plates, can be missed
by the ordinary page-text recognition profile. **Find vehicle plates** uses a
separate region-detection and single-line recognition profile, described below.
Synthetic printed-image tests exercise real local recognition, while controlled
engine fixtures cover failure and publication paths. They do not establish
recognition accuracy on real investigation material. Transcription defaults to
the configured private whisper.cpp runtime, with Cloudflare as a separately
selected and approved option. Routine tests mock inference; see
[local analysis](local-analysis.md) for real-model qualification.

## Source anchors and interpretation

### Vehicle plate candidates

Image uploads and **Run checks again** queue **Find vehicle plates** with
the other local checks. To run it alone, choose it in the case's
**Additional analysis** picker. Select one original image per run. Earlier
results are retained; no original or vehicle record is changed.

Method version 2 uses headless OpenCV to find convex, approximately horizontal
quadrilaterals in a metadata-free preview of at most 2048 pixels on its longest
side. It examines at most 1000 contours, suppresses near-duplicate outlines,
keeps at most five candidate regions and reports when either limit is reached. These geometric
regions are candidates, not a trained plate classifier. Signs, windows and other
rectangular objects can be mistaken for plates; no detected region does not prove
that no plate is present.

Each candidate is mapped back to the original unrotated pixel space and cropped
from the original resolution before perspective correction. The rectified panel
is capped at 1024 × 512 pixels. Three bounded variants read its central vertical
band (22–80%): 2× with full width, then 2× and 3× with a 2% horizontal inset to
reduce frame edges. Grayscale autocontrast, Lanczos resizing and a 20-pixel white
border precede JPEG quality-95 encoding. Recognition input is capped at 1200 × 400
pixels before the border. Tesseract uses English, single-line segmentation
(`--psm 7`) and disabled word dictionaries. It makes at most 15 recognition calls
inside the existing parser CPU, memory, output and wall-clock limits.

Each region receives a **Plate-likeness** score from 0–100, separate from its
Tesseract reading scores. The fixed `plate-likeness.v1` recipe gives up to 20
points for rectangularity/aspect ratio, 60 for a row of character-like connected
components, 10 for OCR readability and 10 for agreement between preprocessing
variants. The character-row measurement uses the first recognition JPEG with
its white border removed, Otsu thresholding in both polarities, component size,
horizontal coverage and median baseline deviation. It requires 3–12 components
and bounded foreground density. Agreement compares ASCII letters/digits with
case and punctuation ignored, without altering any retained reading.

This is a manually weighted ranking heuristic, not a probability or a trained
plate classifier. Regions below 50 are hidden by default. **Show weak regions**
reveals their retained readings, scores, signals and private previews; hiding
them again does not delete anything. Direct finding links reveal the requested
weak region without renumbering findings or moving reviews. Filtered review
counts describe visible findings. All raw findings remain in **Download results**.
Text-bearing signs can still pass, and blurred, obscured or unfamiliar plates
can fall below the threshold. These checks do not establish general accuracy.

Above-threshold readings with at least three ASCII letters/digits are retained
as searchable text candidates, including unexpected punctuation and conflicting
alternatives. Weak-region raw OCR remains in context instead of extracted text,
so it is excluded from search, evidence highlights and downstream extraction
inputs. Shorter output is also retained as context. Unscored historical results
keep their original presentation. Queued version-1 plate runs need a new run
rather than rewriting their retained method or history. The result is
always **Partial** because geometric detection cannot establish exhaustive plate
coverage. Candidates are searchable and reviewable; they never create or update
vehicle records or establish identity, ownership, presence or association.

Every reading retains original region corners and bounding rectangle, crop and
resize settings, parser/detector/engine versions, minimum word score, and hashes
of the original and exact recognition JPEG. Its private processed crop appears
beside the text; **Show region on source image** reveals the original-region
preview. Crops use the existing case-authorized private artifact routes and
immutable result history. Up to 1 MiB of processed crop previews is returned by
the parser, subject to the existing cumulative 2 MiB retained-preview budget;
omissions are disclosed without dropping readings or provenance. No character substitution, jurisdiction-specific guessing, or automatic conflict
resolution is performed. Multi-picture JPEGs search only the primary picture.

### Other extraction anchors

Native PDF text uses one-based page numbers. The extraction order follows the
native PDF content stream; it may disagree with visual reading order or hidden
text. Open/download the original to compare the cited page. Native extraction
does not render PDF pages or execute embedded content. Scanned pages need a
separately retained JPEG/PNG page image for this OCR input profile.

OCR prepares a JPEG of at most 2048 pixels on its longest side, with EXIF and XMP
omitted from recognition input. Transparency is composited on white. Normalized
regions refer to the original image's
**unrotated pixel coordinate space**. The preparation resizes pixels without
applying EXIF orientation or cropping. The original dimensions and preparation
version remain in the finding. Rotation is not corrected automatically.
Unrecognized and unsupported inputs remain distinct from extracted candidate text.

Region inspection reuses that exact prepared JPEG as a private derived preview,
so the normalized rectangles align with the recognized pixels. A run retains at
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
The worker kills/reaps the parser process group, including native OCR children,
after 20 seconds; child CPU is limited to 15 seconds,
core dumps are disabled, and open descriptors are bounded. Parser output goes
to a private temporary file subject to the child's file-size limit. The worker
reads at most 8 MiB plus one byte before parsing it and closes the file on
success or failure. Linux workers enforce a 1 GiB address-space limit.
macOS does not support the same
address-space limit here; the development runner samples process-group RSS and terminates
it above 1 GiB. That sampled check is not a hard instantaneous memory limit.

Additional format limits apply: at most 100 PDF pages, 4 MiB per decompressed
content stream and 16 MiB accumulated page streams, 2 MiB derived text, 50 million
image pixels, 256 KiB extracted image metadata, and 30 minutes of decoded WAV.
Decompression warnings fail closed. Unsupported encodings, encrypted PDFs,
malformed/truncated sources and limits remain visible outcomes or recoverable
failures; they never replace originals or publish an incomplete run as complete.

These subprocess limits provide resource isolation, not an OS security sandbox.
Keep the deployed worker unprivileged and confined, with private storage and
restricted network access. Local OCR invokes only the installed Tesseract
executable, with a 15-second per-command timeout, one native thread, bounded private temporary stdout and
discarded stderr. It receives a prepared JPEG through stdin, never an evidence
path or arbitrary input list. The parser environment excludes operator secrets.
A missing engine or English data produces a safe error with operator recovery
instructions and no remote fallback. Other ML/LLM inference uses the shared
backend boundary, defaulting to pinned private runtimes. External processing uses
the authenticated Cloudflare AI Gateway only when separately selected and approved.
Both routes check current case access immediately before and after requests.
Local failure never triggers an external request. No direct public-provider
endpoint is called by these extractors.
The existing job/result workflow handles retries, revocation and atomic
publication; each extraction retains its parser/model and source versions.

Primary references:

- [pypdf text extraction and content-stream memory limits](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
- [Pillow image metadata APIs](https://pillow.readthedocs.io/en/stable/reference/Image.html) and [parser security considerations](https://pillow.readthedocs.io/en/stable/handbook/security.html).
- [Cloudflare Workers AI Whisper input/output profile](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/).
- [Tesseract command line and TSV output](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
- [Tesseract installation and language data](https://tesseract-ocr.github.io/tessdoc/Installation.html).
