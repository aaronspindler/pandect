# Local analysis and private inference runtimes

Pandect defaults to **Local** for deliberately selected model-based analysis.
Local includes the Pandect host and an explicitly configured operator-controlled
private LAN service. Cloudflare remains an optional, separately selected and
approved external route. A missing model, failed runtime or context limit never
sends evidence to the cloud automatically.

Printed-English OCR, native document text, metadata inspection, rule-based
identifiers, keywords, writing comparisons, conversation timing and spatial
calculations already run locally. **Analyze evidence** keeps its existing
deterministic/OCR batch; it does not automatically add LLM analysis or transcription.

## Current methods and limits

| Method | Local implementation | Qualification needed |
| --- | --- | --- |
| Recording transcription | whisper.cpp with `large-v3-turbo`; existing English PCM WAV profile, 30-second chunks and original-timeline anchors. | Recognition and timing on names, accents, overlap and non-speech. No diarization or speaker identity. |
| Topics and short summaries | llama.cpp/Qwen3-8B with exact quotation validation. Keyword-only analysis remains independent. | Topic relevance, omitted qualifications and unsupported interpretation. |
| Conversation summaries | The existing message-aware session chunks through the same local text runtime. | Coverage, conflicting messages and missing context. Current session grouping selects text messages. |
| Claims, plans and commitments | Structured candidate statements with exact source quotations. | Attribution, negation, hearsay, tense and cancellation. |
| Semantic conversation links | Two-sided quotation citations through the local text runtime. | False links and ambiguous references; candidates require investigator review. |
| Geocoding | Operator-controlled Nominatim-compatible service. | Loaded area, dataset freshness, ambiguous/no matches and candidate review. |
| Geographic context | Operator-controlled Overpass-compatible service; geometry matching already runs locally. | Loaded area, map timestamps, retained snapshots and actual historical coverage. |

These are supported execution paths, not claims of model accuracy or equivalence
to the remote 70B model. The baseline text model is
[Qwen3-8B Q4_K_M](https://huggingface.co/Qwen/Qwen3-8B-GGUF) in non-thinking mode.
The optional CPU-first image pins llama.cpp **v0.8.0** at
`8642541b255bfb5dc39cd351a178be09013a55d6` and whisper.cpp **v1.9.5** at
`d1be6fde11ac6e0407606b4e42fe72d34add8037`. Model licenses are separate from
runtime licenses: the selected Qwen artifact is Apache-2.0 and Whisper is MIT.
Retain upstream model metadata/license notices with the operator's model files.

## Start the optional CPU runtimes

The ordinary Compose stack works without these services. Model files are never
downloaded during application startup or inference. First obtain the single-file
Q4_K_M artifact from the official Qwen repository and the
[Whisper large-v3-turbo weights](https://huggingface.co/ggerganov/whisper.cpp/tree/main)
for whisper.cpp. Verify their SHA-256 against upstream file metadata and retain
the source revision. Put the reviewed files at ignored `data/models/text.gguf`
and `data/models/transcription.bin`, or configure the two `*_MODEL_FILE` paths.
Do not place models under the source-controlled `inference/` directory.

After the normal local setup has created `.env`, this initial configuration reads
the model bytes, records their digests, and adds a private runtime token without
printing it:

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

The setup example uses the default file names; use your configured paths when
computing hashes for alternative files. Rerunning it rotates the runtime token,
so recreate web, worker and both runtimes together. It does not reset any database
or evidence. Missing files, digest mismatches and missing tokens prevent runtime
startup. No configured inference URL means analysis reports a recoverable
configuration error; it does not disable case management.

Each runtime verifies its mounted, read-only model before launching the pinned
native engine. Its authenticated `/identity` reports the model digest and engine
revision. Before evidence is sent, the application compares that identity with
the queued execution snapshot. Every POST also supplies the expected identity;
the runtime compares it under its request lock before reading/forwarding content,
so replacing an engine under the same model alias cannot silently change a run.
The native engine listens only on the runtime
container's loopback interface. Its front door exposes only identity, readiness,
and the concrete inference/tokenization routes required by Pandect.

The `inference` Docker network is internal, with no published ports and no
internet route for either runtime. Web/worker also join it. Runtime containers
have a read-only filesystem, private temporary memory, no evidence-storage mount,
no Gateway credentials, no privileged capabilities and no payload logging.
They accept one request at a time. A request deadline kills and reaps the native
process group; the container restarts and reloads its verified model. A cancelled
case run withholds publication; an already submitted local request can continue
until it finishes or reaches that deadline.

## Configuration and capacity

Set identical application configuration on web and worker. `*_URL` explicitly
designates an operator-controlled inference endpoint; a private-looking hostname
is not an automatic trust decision. Only the supplied private front door and its
identity contract are supported. For a separate LAN inference host, use TLS or a
trusted encrypted tunnel and firewall it to the Pandect workers. Do not publish
the runtimes to an untrusted network.

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

Q4_K_M text weights alone are about 5 GB. Context/KV caches, scratch buffers and
the application/database add memory. A 16–24 GB inference allocation is a
planning allowance, not a measured requirement or a throughput guarantee. GPU
inference is optional: a separately qualified private native runtime can retain
the same identity/API contract. The supplied reproducible image is CPU-only and
does not silently offload work or select a smaller model.

For an NVIDIA deployment, build a separately pinned image with the same
`server.py`/`revisions.json` and native source commits, `-DGGML_CUDA=ON` for both
native builds, the matching CUDA runtime libraries, and the required host GPU
driver. Keep model files, authentication, read-only storage and the internal
network unchanged. After qualifying that image, use a Compose override such as:

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

Save it outside source control as `/tmp/pandect-gpu.yaml`, set
`PANDECT_LOCAL_GPU_IMAGE` to the reviewed image digest, and start with:

```sh
docker compose -f compose.yaml -f /tmp/pandect-gpu.yaml --profile local-ai up -d --no-build
```

The two GPU controls only change offload; they do not add GPU support
to the CPU image. No GPU driver/image or throughput is claimed as qualified by
the CPU build.

The text client counts the fully rendered prompt with the engine tokenizer and
reserves output tokens. Oversized content fails with a request to narrow the
selection; it is never silently truncated. Session summaries retain the existing
message-preserving chunks. Local inference runs separately from the file
parser's 1 GiB/20-second boundary. Deterministic extraction retains that boundary.

Starting a new run records the selected backend, model artifact, runtime,
inference settings, adapter version and location. Request/interpretation changes
must update the adapter version. Historical runs remain unchanged. Queued model
or endpoint drift fails recoverably; start another run to select new settings.
Adapter version 2 shares the local/Gateway evidence prompt and strict JSON rules:
nonfinite numbers (including exponent overflow) and malformed output are rejected.
Queued inference runs retaining version 1 fail with `execution_changed` before
dispatch; after upgrading both web and worker, start a new run to record version 2.
Cloud requests retain their separate `GatewayRequest` accounting and approval.
There is no implicit local-to-cloud failover.

## Reuse private map services

Geocoding and map context already accept Nominatim/Overpass-compatible private
URLs. After verifying the endpoint is operated under your control, set
`PANDECT_GEOCODER_LOCAL=true` or `PANDECT_MAP_CONTEXT_LOCAL=true`. Otherwise each
lookup requires external-processing approval. Record source name, dataset version
and attribution as described in the focused geographic guides.

The existing Spindlers homelab configuration uses the `misc-containers` VM,
Overpass at `http://192.168.1.246:8081/api/interpreter`, and Nominatim at
`http://192.168.1.246:8082`. Reuse its guarded import/recovery procedures rather
than deploying duplicate services. The latest recorded planet import on
2026-10-28 failed from memory exhaustion on an 8 GB VM; completion is unverified.
Qualify actual queries, dataset timestamp, replication freshness, restart,
storage and recovery before connecting Pandect. Low query traffic does not
remove import costs. Do not replace intended worldwide coverage with regional
data silently or introduce an arbitrary Pandect RAM gate.

That Overpass deployment has no historical attic data. Leave
`PANDECT_MAP_CONTEXT_HISTORY_START` empty until historical coverage is genuinely
provisioned and tested. Current OSM data cannot reconstruct old map states or
establish real-world conditions. Reference snapshots and geocode reviews remain
retained independently of service availability.

Background tiles are separate from analysis. An empty `PANDECT_MAP_TILE_URL`
disables the basemap. Operators may configure a private or public HTTPS raster
template. All configured tiles load automatically when the map opens, without a
consent step; the provider receives the browser's IP/site origin and viewed tile
areas. Evidence files, titles and source details stay in Pandect. Markers and the
accessible location list remain available without tiles. See [maps](maps.md).

## Qualification and recovery

Routine automated tests use synthetic fixtures and mocked inference; they do not
download models or establish recognition or reasoning quality. After provisioning
reviewed model artifacts, run a separate qualification on synthetic or sanitized
material, with third-party egress blocked and only the private endpoints allowed:

```sh
uv run --locked --env-file .env python manage.py evaluate_local_analysis > /tmp/local-analysis-qualification.json
# Optional supported WAV, at most 16 MiB / 90 seconds:
uv run --locked --env-file .env python manage.py evaluate_local_analysis --audio /absolute/synthetic.wav > /tmp/local-analysis-with-audio.json
# Qualify just one method; repeat --method to select several:
uv run --locked --env-file .env python manage.py evaluate_local_analysis --method transcription --audio /absolute/synthetic.wav > /tmp/local-transcription.json
```

The command runs all four text modes through the real local adapters and existing
citation validators on fixed synthetic messages. It writes no database records,
makes no Gateway calls, and exits nonzero if any selected method fails. Optional
audio uses the existing PCM parser and chunk/timeline path. Its JSON includes
expected distinctions, actual validated output, usage and elapsed time, without
assigning an unsupported quality grade. Keep audio/output appropriately private.

1. Exercise all seven paths. Confirm external AI and geographic processing require
   the appropriate separate choice/approval. Disable the basemap or configure a
   private tile provider for an egress-blocked qualification; configured public
   tiles load automatically when viewing the map.
2. Transcribe known speech with names, accents, overlap and non-speech. Inspect
   word errors, omissions and original-timeline intervals. Score against the
   retained transcript; unavailable confidence scores stay unknown.
3. Evaluate topics, summaries, claims and links separately against analyst-written
   expected findings, including negation, conflicting messages, hearsay,
   cancellation, repeated quotations and deliberately ambiguous references.
   Report citation validity separately from interpretation precision and recall.
4. Record model/runtime digests, hardware, prompt/context sizes, cold/warm latency,
   peak process/container memory and queue completion time. Start with one active
   inference request; do not infer capacity from model-file size.
5. Test missing models, wrong hashes, engine crashes, deadline termination,
   oversized context, cancellation and revoked case access between chunks.
   No invalid or unauthorized result may be published.
6. Qualify map no-match/ambiguity, data freshness, current versus historical
   coverage, exact retained snapshots, restart and recovery independently.

An exact quotation validates its source anchor, not the interpretation. Keep
model outputs labelled as suggestions and preserve unresolved outcomes. Do not
advertise parity with remote inference or worldwide map readiness from mocked
tests. Record per-method qualification results with the deployment, including
any disabled or experimental modes.

### Recorded synthetic qualification, 2026-10-29

All five inference paths completed against the real pinned engines and verified
model files. Forward/reverse geocoding and geographic context also completed
against a synthetic private HTTP service, including real geometry parsing and a
validated retained reference. The Docker network was internal; an external TCP
probe failed. No investigation records or Gateway requests were created.

| Method | Observed result | Elapsed time |
| --- | --- | --- |
| Topics/short summaries | Six model suggestions had valid quotations, but an ambiguous place became a specific venue and labels repeated source types. Interpretation remains unqualified. | 314 s |
| Conversation summaries | Five message summaries had valid quotations and preserved cancellation/hearsay; an ambiguous venue was incorrectly resolved. Interpretation remains unqualified. | 174 s |
| Claims/events | Six quoted suggestions preserved ambiguity and qualifications, but some event/relationship classifications were weak. They still require review. | 173 s |
| Semantic conversation links | All six model suggestions cited only one source. The guard retained them as unresolved; no completed two-sided model link was demonstrated. | 193 s |
| Transcription | The words in a 3.565-second generated English recording matched the supplied script. Two source-timed segments and two unresolved gaps produced a partial result. This is not a general recognition-quality measurement. | 20.6 s |
| Geocoding | Forward and reverse HTTP lookups returned the synthetic candidate without external approval. | Not benchmarked |
| Geographic context | Private HTTP lookup, bounded parsing and the retained reference passed against the synthetic map response. | Not benchmarked |

The CPU-only Linux/arm64 runtimes had four CPUs each on a 16 GiB host with an
approximately 7.75 GiB Docker VM. Text trials used a **4,096-token context and
1,024 output tokens**, below the configured defaults. Observed native-process
high-water RSS reached approximately **5.96 GiB for text** and **1.70 GiB for
transcription**. The initial topic trial overlapped builds/tests; later text
trials used the warm runtime. Runtimes were evaluated serially; combined-service
memory was not qualified. These observations do not establish production
throughput, maximum-context capacity, accuracy rates or remote-model parity.

The completed execution paths are usable for operator qualification and reviewed
suggestions. The 8B model is not qualified for dependable interpretation or
semantic linking. Keep ambiguous references unresolved and reject unsupported
specificity even when quotation anchors validate. Larger/different models and
GPU execution need their own evaluation.

Raw synthetic results are retained locally in ignored
`data/validation/local-analysis/2026-10-29-qualification.json`; verified model
files and source/checksum metadata are in ignored `data/models/`. The initial
multi-method run lost runtime availability during an interrupted session;
separate completed trials supersede those interrupted method results.
The existing homelab endpoints were unreachable from the validation host, so
worldwide coverage, replication and historical data remain unqualified. The
application environment and running application stack were not changed.

Recover local failures by correcting configuration/capacity and starting another
run. Do not edit retained runs, force-reset task rows, or delete originals.
Restart a stopped runtime with `docker compose --profile local-ai restart
local-text local-transcription`. Keep model files and their source/checksum
metadata in operator backups separately from evidence; keep PostgreSQL/private
storage backups coordinated as described in [operations](operations.md).

Primary runtime references: [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/8642541b255bfb5dc39cd351a178be09013a55d6/tools/server/README.md),
[whisper.cpp server](https://github.com/ggml-org/whisper.cpp/blob/d1be6fde11ac6e0407606b4e42fe72d34add8037/examples/server/README.md),
[Whisper limitations](https://huggingface.co/openai/whisper-large-v3-turbo),
[Nominatim administration](https://nominatim.org/release-docs/latest/admin/Installation/),
and [Overpass own-instance setup](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html).
