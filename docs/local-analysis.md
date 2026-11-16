# Local analysis and private inference runtimes

Model analysis defaults to **Local**: the Pandect host or an explicitly configured
operator-controlled private LAN service. This includes confirmed CSV message
imports' automatic suite. Cloudflare requires separate selection/approval; model,
runtime or context failures never trigger cloud fallback.

Printed-English OCR, native text extraction, metadata, rule-based identifiers,
keywords, writing comparisons, timing and spatial calculations run locally through
the [local batch](analysis.md#retained-processing). CSV message imports additionally
queue bounded topics, claims, session summaries and semantic exchanges. Other
LLM analysis/transcription remains deliberate. Missing inference leaves recoverable
runs without undoing messages or independent results.

## Current methods and limits

| Method | Local implementation | Qualification needed |
| --- | --- | --- |
| Recording transcription | whisper.cpp with `large-v3-turbo`; existing English PCM WAV profile, 30-second chunks and original-timeline anchors. | Recognition and timing on names, accents, overlap and non-speech. No diarization or speaker identity. |
| Topics and short summaries | llama.cpp/Qwen3-8B with exact quotation validation. Keyword-only analysis remains independent. | Topic relevance, omitted qualifications and unsupported interpretation. |
| Conversation summaries | The existing message-aware session chunks through the same local text runtime. | Coverage, conflicting messages and missing context. Current session grouping selects text messages. |
| Claims, plans and commitments | Structured candidate statements with exact source quotations. | Attribution, negation, hearsay, tense and cancellation. |
| Semantic conversation links | Two-sided quotation citations through the local text runtime. | False links and ambiguous references; candidates require investigator review. |
| Question-driven synthesis | Selected text and findings through the local text runtime, with distinct original and prior-finding citations. See [synthesis](synthesis.md) for selection and output bounds. | Supporting/contrary evidence, negation, ambiguity, unknowns and abstention. Valid quotations do not establish interpretation quality. |
| Geocoding | Operator-controlled Nominatim-compatible service. | Loaded area, dataset freshness, ambiguous/no matches and candidate review. |
| Geographic context | Operator-controlled Overpass-compatible service; geometry matching already runs locally. | Loaded area, map timestamps, retained snapshots and actual historical coverage. |

Execution support does not establish accuracy or equivalence to the remote 70B
model. The baseline is [Qwen3-8B Q4_K_M](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
in non-thinking mode. The optional CPU image pins llama.cpp **v0.8.0** at
`8642541b255bfb5dc39cd351a178be09013a55d6` and whisper.cpp **v1.9.5** at
`d1be6fde11ac6e0407606b4e42fe72d34add8037`. Qwen's Apache-2.0 and Whisper's MIT
model licenses are separate from runtime licenses. Retain upstream metadata/notices
with model files.

## Start the optional CPU runtimes

The ordinary stack needs no inference services. Startup/inference never downloads
models. Obtain the official single-file Qwen Q4_K_M artifact and
[Whisper large-v3-turbo weights](https://huggingface.co/ggerganov/whisper.cpp/tree/main).
Verify SHA-256 against upstream metadata and retain the source revision. Store
reviewed files in ignored `data/models/text.gguf` and
`data/models/transcription.bin`, or set `*_MODEL_FILE`; never put them under
source-controlled `inference/`.

After [local setup](development.md#first-run) creates `.env`, record model digests
and add a private runtime token without printing it:

```sh
uv run --locked python - <<'PY'
import hashlib
import secrets
from pathlib import Path

values = {
    "PANDECT_LOCAL_TEXT_URL": "http://local-text:8080",
    "PANDECT_LOCAL_TRANSCRIPTION_URL": "http://local-transcription:8080",
    "PANDECT_LOCAL_AI_TOKEN": secrets.token_urlsafe(32),
}
for purpose, name in (("TEXT", "text.gguf"), ("TRANSCRIPTION", "transcription.bin")):
    with (Path("data/models") / name).open("rb") as stream:
        values[f"PANDECT_LOCAL_{purpose}_SHA256"] = hashlib.file_digest(stream, "sha256").hexdigest()
path = Path(".env")
lines = path.read_text().splitlines()
lines = [line for line in lines if line.split("=", 1)[0] not in values]
path.write_text("\n".join(lines + [f"{key}={value}" for key, value in values.items()]) + "\n")
path.chmod(0o600)
PY
docker compose --profile local-ai build local-text local-transcription
docker compose --profile local-ai up -d --wait
```

Use configured paths when hashing alternative files. Rerunning rotates the token;
recreate web, worker and both runtimes together. No database/evidence is reset.
Missing files/tokens or digest mismatches block runtime startup. Unconfigured URLs
produce recoverable analysis errors; case management stays usable.

Runtimes verify read-only mounted models before launching pinned engines. Changes
during verification are rejected; later changes/unavailability stop the engine.
Authenticated `/identity` reports model digest/engine revision. Before sending
evidence, Pandect compares these against the queued snapshot. Each POST carries
expected identity, checked under the runtime request lock before content is read
or forwarded. Replacing an engine under an alias cannot silently change a run.
Native engines listen only on container loopback; the front door exposes only
identity, readiness and required inference/tokenization routes.

The internal `inference` network joins web/workers and runtimes, with no published
ports or runtime internet route. Runtimes have read-only filesystems, private
temporary memory, no evidence mount/Gateway credentials/privileged capabilities,
and no payload logging. Each accepts one request at a time. Deadlines kill/reap
the native process group; container restart reloads the verified model.
Cancellation withholds publication, but submitted requests may continue until
completion/deadline.

## Configuration and capacity

Set identical web/worker values. `*_URL` explicitly designates operator control;
a private hostname alone does not. Only the supplied front door/identity contract
is supported. LAN services need TLS or a trusted encrypted tunnel and a firewall
restricting access to Pandect workers. Never expose runtimes to untrusted networks.

| Variable | Default / purpose |
| --- | --- |
| `PANDECT_LOCAL_TEXT_URL`, `PANDECT_LOCAL_TRANSCRIPTION_URL` | Empty disables the corresponding runtime; Compose examples are above. |
| `PANDECT_LOCAL_TEXT_MODEL`, `PANDECT_LOCAL_TRANSCRIPTION_MODEL` | `Qwen3-8B-Q4_K_M`, `whisper-large-v3-turbo`; must match runtime identity. |
| `PANDECT_LOCAL_TEXT_SHA256`, `PANDECT_LOCAL_TRANSCRIPTION_SHA256` | Required exact model-file hashes. |
| `PANDECT_LOCAL_TEXT_RUNTIME`, `PANDECT_LOCAL_TRANSCRIPTION_RUNTIME` | Pinned engine identity; change deliberately with a reviewed image update. |
| `PANDECT_LOCAL_AI_TOKEN` | Required private runtime bearer token; never retained in run parameters. |
| `PANDECT_LOCAL_TEXT_CONTEXT_TOKENS` | 32,768, including the full rendered prompt and output allowance. |
| `PANDECT_LOCAL_AI_MAX_OUTPUT_TOKENS` | 4,096. |
| `PANDECT_LOCAL_AI_TIMEOUT` | 600 seconds per request, capped at 1,800. |
| `PANDECT_LOCAL_AI_MAX_REQUEST_BYTES`, `PANDECT_LOCAL_AI_MAX_RESPONSE_BYTES` | 8 MiB / 2 MiB. |
| `PANDECT_LOCAL_AI_THREADS`, `PANDECT_LOCAL_AI_CPUS` | Four native threads / four Compose CPUs per service. |
| `PANDECT_LOCAL_TEXT_MEMORY`, `PANDECT_LOCAL_TRANSCRIPTION_MEMORY` | Compose memory ceilings of `24g` / `4g`; configurable, not host minimum checks. |

Text weights alone occupy about 5 GB; context/KV caches, buffers, web and database
need more. A 16–24 GB inference allowance is a planning estimate, not a measured
requirement or throughput guarantee. The reproducible image is CPU-only and never
silently offloads or selects a smaller model. Separately qualified GPU runtimes
can preserve the identity/API contract.

For NVIDIA, separately pin an image retaining `server.py`/`revisions.json` and
native commits. Build both with `-DGGML_CUDA=ON`, matching CUDA libraries and host
driver. Preserve model/authentication/read-only/internal-network configuration.
After qualification, use an override such as:

```yaml
services:
  local-text:
    image: ${PANDECT_LOCAL_GPU_IMAGE:?Set a qualified image digest}
    gpus: all
    environment:
      PANDECT_RUNTIME_GPU_LAYERS: "99"
  local-transcription:
    image: ${PANDECT_LOCAL_GPU_IMAGE:?Set a qualified image digest}
    gpus: all
    environment:
      PANDECT_RUNTIME_GPU: "true"
```

Save outside source control as `/tmp/pandect-gpu.yaml`, set
`PANDECT_LOCAL_GPU_IMAGE` to the reviewed digest, then run:

```sh
docker compose -f compose.yaml -f /tmp/pandect-gpu.yaml --profile local-ai up -d --no-build
```

GPU controls enable offload only; they add no GPU support to the CPU image.
The CPU build qualifies no GPU driver/image or throughput.

The text client tokenizes the full rendered prompt and reserves output tokens.
Oversized selections fail with narrowing instructions, never silent truncation;
session chunks preserve messages. Inference runs outside the deterministic
parser's 1 GiB/20-second boundary.

New runs retain backend, artifact, runtime, settings, adapter version and location.
Request/interpretation changes require adapter-version changes, preserving history.
Queued model/endpoint drift fails recoverably; select new settings in a new run.
Adapter version 2 shares local/Gateway prompts and strict JSON validation,
rejecting nonfinite numbers (including exponent overflow) and malformed output.
Queued version-1 inference fails with `execution_changed` before dispatch; upgrade
web/worker together and start a version-2 run. Cloud requests retain separate
`GatewayRequest` accounting/approval; there is no implicit failover.

## Reuse private map services

Geocoding/context accept Nominatim/Overpass private URLs. Verify operator control
before setting `PANDECT_GEOCODER_LOCAL=true` or `PANDECT_MAP_CONTEXT_LOCAL=true`;
otherwise lookup requires external approval. Record source name/version/attribution
as specified in the geographic guides.

Verify query behavior, coverage, dataset/replication freshness, restart and recovery
before connection. Size initial imports separately from routine queries and disclose
regional limits. Leave `PANDECT_MAP_CONTEXT_HISTORY_START` empty without verified
history. Current OSM cannot reconstruct historical states or prove real-world
conditions. Retained snapshots/reviews survive service outages.

Basemap tiles are separate: configured tiles, including public tiles, load
automatically. For blocked-egress qualification, leave `PANDECT_MAP_TILE_URL` empty
or use a private provider; see [map privacy](maps.md#external-tiles-and-privacy).

## Qualification and recovery

Routine synthetic/mocked tests neither download models nor establish quality.
Provision reviewed artifacts, then qualify synthetic/sanitized material with
third-party egress blocked and only private endpoints allowed:

```sh
uv run --locked --env-file .env python manage.py evaluate_local_analysis > /tmp/local-analysis-qualification.json
# Optional supported WAV, at most 16 MiB / 90 seconds:
uv run --locked --env-file .env python manage.py evaluate_local_analysis --audio /absolute/synthetic.wav > /tmp/local-analysis-with-audio.json
# Qualify just one method; repeat --method to select several:
uv run --locked --env-file .env python manage.py evaluate_local_analysis --method transcription --audio /absolute/synthetic.wav > /tmp/local-transcription.json
```

The command runs listed text methods through real local adapters/citation validators
on fixed synthetic messages. It writes no database records, calls no Gateway and
exits nonzero on any selected failure. Optional audio exercises PCM parsing and
chunk/timeline handling. JSON records expected distinctions, validated output,
usage and elapsed time without assigning a quality grade. Keep audio/output private.

1. Exercise every path and required external AI/geographic choice/approval;
   control basemap egress too.
2. Transcribe names, accents, overlap and non-speech. Compare errors, omissions
   and timeline intervals against retained transcripts; absent confidence is unknown.
3. Evaluate each text method against analyst expectations: supporting/contrary
   evidence, negation, conflict, hearsay, cancellation, repeated quotes, ambiguous
   references and abstention. Separate citation validity from interpretation
   precision/recall.
4. Record model/runtime digests, hardware, prompt/context sizes, cold/warm latency,
   peak process/container memory and queue completion. Begin with one active
   request; file size does not establish capacity.
5. Test missing/wrong models, crashes, deadline termination, oversized context,
   cancellation and access revocation between chunks. Publish no invalid or
   unauthorized result.
6. Independently qualify map ambiguity/no-match, freshness, current/history
   coverage, exact snapshots, restart and recovery.

Exact quotations validate anchors, not interpretation. Label suggestions, preserve
unresolved outcomes and record per-method deployment qualification, including
disabled/experimental modes. Mocked tests prove neither remote-inference parity
nor worldwide map readiness.

The baseline 8B model remains unqualified for dependable interpretation/semantic
linking. The [29 October trial](archive/local-inference-qualification-2026-10-29.md)
found unsupported specificity despite valid quotes and excluded question synthesis.
It qualifies no current model/runtime, capacity or map service. Different models
and GPU execution require separate evaluation.

Correct configuration/capacity and start another run to recover. Never edit
retained runs, force-reset tasks or delete originals. Restart stopped runtimes
with `docker compose --profile local-ai restart local-text local-transcription`.
Back up models/source/checksum metadata separately from evidence; coordinate
PostgreSQL/private storage backups as described in [operations](operations.md).

Primary references: [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/8642541b255bfb5dc39cd351a178be09013a55d6/tools/server/README.md),
[whisper.cpp server](https://github.com/ggml-org/whisper.cpp/blob/d1be6fde11ac6e0407606b4e42fe72d34add8037/examples/server/README.md),
[Whisper limitations](https://huggingface.co/openai/whisper-large-v3-turbo),
[Nominatim administration](https://nominatim.org/release-docs/latest/admin/Installation/)
and [Overpass setup](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html).
