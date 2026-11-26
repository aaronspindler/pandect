# Analysis on the Pandect host

Pandect keeps originals, provenance and saved findings in the case store. The background worker handles bounded binary decoding, native PDF and email text, metadata, and deterministic checks. These operations remain available when the case owner disables external AI.

Model-based work goes through Cloudflare. The case owner reviews the search account, namespace, investigation Agent and model destinations in [Case settings](cases.md#external-ai-and-hosted-search). Configure [AI Gateway](analysis.md#external-ai-configuration) on both web and worker. Archived local-model results retain the execution details originally recorded.

Native text extraction records a coverage gap for PDF pages without usable text. The separately approved OCR operation can submit only those rendered pages, or a metadata-free image, to the vision model. Transcription uses bounded audio chunks. File inspection and format conversion run in the isolated parser before model submission.
