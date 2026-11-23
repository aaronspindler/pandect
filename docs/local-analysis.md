# Local analysis and private inference runtimes

Supported built-in analysis runs locally. New evidence also queues supported
inference using the [case's selected AI backend](cases.md#ai-backend), with
captured execution settings. Runtime failures stay with the selected backend.

## Current methods and limits

| Method | Local implementation | Qualification needed |
| --- | --- | --- |
| Recording transcription | whisper.cpp with `large-v3-turbo`; English PCM WAV or AAC-in-M4A recordings, 30-second WAV chunks and original-timeline anchors. | Recognition and timing on names, accents, overlap and non-speech. No diarization or speaker identity. |
| Topics and short summaries | Ollama/Qwen3.5 with exact quotation validation. Keyword-only analysis remains independent. | Topic relevance, omitted qualifications and unsupported interpretation. |
| Conversation summaries | The existing message-aware session chunks through the same local text runtime. | Coverage, conflicting messages and missing context. Current session grouping selects text messages. |
| Claims, plans and commitments | Structured candidate statements with exact source quotations. | Attribution, negation, hearsay, tense and cancellation. |
| Claim comparison | Selected message or call transcript wording and retained text evidence through the private text runtime, with checked source quotations. | Supporting and conflicting material, ambiguous references, and useful abstention. |
| Semantic conversation links | Two-sided quotation citations through the local text runtime. | False links and ambiguous references; candidates are suggestions. |
| Retained question-driven synthesis | Existing runs retain distinct original and prior-finding citations; the new-question form has been removed. See [synthesis](synthesis.md) for processing and output bounds. | Supporting/contrary evidence, negation, ambiguity, unknowns and abstention. Valid quotations do not establish interpretation quality. |
| Geocoding | Operator-controlled Nominatim-compatible service. | Loaded area, dataset freshness, ambiguous/no matches and candidate review. |
| Geographic context | Operator-controlled Overpass-compatible service; geometry matching already runs locally. | Loaded area, map timestamps, retained snapshots and actual historical coverage. |

Execution support does not establish accuracy or equivalence to the remote 70B
model. Local text uses an operator-provisioned Qwen3.5 model in Ollama, with
`qwen3.5:9b` as the default model name. The optional CPU transcription image pins
whisper.cpp **v1.9.5** at `d1be6fde11ac6e0407606b4e42fe72d34add8037`.
Model licenses are separate from runtime licenses; retain upstream
metadata/notices with provisioned artifacts.

Recording preparation uses the [shared recording profile](original-previews.md#recordings).
The parser produces the PCM used for transcription chunks while retaining the
original recording and its checksum.

## Use Ollama for local text analysis

Ollama is the **Local** text backend for Qwen3.5, including `qwen3.5:9b`.
It supplies topics, claims, claim comparisons, session summaries, semantic links
and processing for retained question-synthesis runs. Transcription uses the
separate Whisper runtime,
and printed-English OCR remains in the bounded Tesseract parser. **External AI**
uses the [configured external provider](analysis.md#external-ai-configuration).

Use Ollama **0.40.11 or later**, with an exact reviewed server version pinned in
Pandect. This adapter needs digest-addressed local models and prompt rendering;
older versions fail closed. Provision the model deliberately:

```sh
ollama pull qwen3.5:9b
```

For Python/web/worker processes running on the host, run this from the checkout
after creating `.env`. It records the installed manifest digest (not the model
weight-file hash) and exact server version without sending evidence or changing
the database:

```sh
uv run --locked python - <<'PY'
import json
from pathlib import Path
from urllib.request import urlopen

url = "http://127.0.0.1:11434"
model = "qwen3.5:9b"
with urlopen(url + "/api/version", timeout=10) as response:
    version = json.load(response)["version"]
with urlopen(url + "/api/tags", timeout=10) as response:
    installed = json.load(response)["models"]
matches = [item for item in installed if item["name"] == model]
if len(matches) != 1 or matches[0].get("remote_host") or matches[0].get("remote_model"):
    raise SystemExit("Install the local qwen3.5:9b model before configuring Pandect.")
values = {
    "PANDECT_LOCAL_TEXT_URL": url,
    "PANDECT_LOCAL_TEXT_MODEL": model,
    "PANDECT_LOCAL_TEXT_SHA256": matches[0]["digest"],
    "PANDECT_LOCAL_TEXT_RUNTIME": "ollama@" + version,
}
path = Path(".env")
lines = [line for line in path.read_text().splitlines()
         if line.split("=", 1)[0] not in values]
path.write_text("\n".join(lines + [f"{key}={value}" for key, value in values.items()]) + "\n")
path.chmod(0o600)
PY
```

Restart both host web and worker processes with the updated `.env`. No
`local-ai` Compose profile or bundled text model file is needed for Ollama.
The native loopback API needs no token; an authenticated private proxy can use
`PANDECT_LOCAL_AI_TOKEN`. Keep identical settings in web and worker.

For web/workers inside Docker Desktop, set `PANDECT_LOCAL_TEXT_URL` to
`http://host.docker.internal:11434` and make Ollama reachable from the Docker VM.
Container loopback points to the container itself. On Linux, supply an explicit
host gateway mapping or a private Ollama service address. Restrict any non-loopback
listener to the Pandect host/network; use TLS or a trusted encrypted tunnel for
LAN access. Recreate the web/worker containers after configuring the endpoint.
The host qualification command below needs the host-reachable URL, supplied as
an environment override if `.env` contains the Docker address.

Pandect verifies the configured tag's digest and server version, then requests
the immutable `sha256:<digest>:local` model. The local-only suffix makes Ollama
reject cloud-backed models server-side. Pandect never pulls a missing model,
changes models, or falls back to cloud. Updating Ollama or pulling a changed tag
requires reviewing and updating the pins, restarting web/worker together, and
starting new runs; queued configuration drift fails recoverably. Keep Ollama's
server/version and model storage under operator control throughout processing.

Requests disable thinking, tools, streaming, truncation and context shifting,
use temperature zero, and unload the model after inference. No conversation
history is shared between runs. Ollama renders the complete prompt first;
Pandect bounds Qwen3.5's byte-level token count conservatively by its rendered
UTF-8 byte length plus eight special-token slots and the output allowance.
This can reject text that would fit an exact token count, but never silently
narrows the selection. Other tokenizer families are unsupported. Results still
pass the shared strict JSON, source-quotation and domain validators; incomplete
output is withheld. Usage retains actual input/output token counts separately
from the conservative context bound.

The application uses a fixed 16,384-token context, including the complete prompt
and output allowance. This does not establish a host's capacity or model accuracy.
Keep one active inference request and measure memory and latency alongside
Pandect's other services. Qualify with synthetic/sanitized text using the existing
`evaluate_local_analysis` command below. Successful execution does not establish
investigation accuracy.

## Case-search embeddings

Set `PANDECT_SEARCH_EMBEDDING_PROVIDER` in both web and worker environments.
The selection applies only to meaning search. Keyword search stays available
with every setting, including `disabled`.

| Provider | What to configure |
| --- | --- |
| `local` (default) | An operator-controlled Ollama service with the four pins below. |
| `openai-gateway` | `PANDECT_AI_ACCOUNT_ID`, `PANDECT_AI_GATEWAY_ID`, `PANDECT_AI_GATEWAY_TOKEN` and `PANDECT_AI_OPENAI_KEY_ALIAS`. Store the OpenAI key behind that alias in Cloudflare AI Gateway. |
| `disabled` | Turn off meaning search and its indexing jobs. |

With `openai-gateway`, Pandect requests 1,536 dimensions from OpenAI
`text-embedding-3-large` through the configured Cloudflare gateway. Vectors stay
in the case database. An owner selects **Approve and start** under **Meaning
search** in Case settings to approve indexing of eligible active, archived and
future sources and explicit query submissions. Changing the approved
destination or model profile requires a fresh approval. The gateway request
disables Cloudflare payload logging and cache reuse. Configure the provider's
retention separately. Each outbound attempt records its case, operation, model,
byte counts, status and provider-reported input tokens when available.

Both profiles use up to 32 verbatim passages per request, each at most 246 UTF-8
bytes. A passage retains its source, offset and text fingerprint. Only complete
documents from the selected profile are searchable. Investigators can request
up to six additional related passages from the first keyword results page.
The hosted profile uses a minimum cosine similarity of 0.4884. Returned
passages can be unrelated even above the cutoff. Inspect the linked source.

### Private case-search embeddings

Set the following for the optional 384-dimensional local profile. Provision
`ollama pull all-minilm:22m` on the private service and inspect `/api/version`
and the installed model's manifest digest in `/api/tags`.

| Setting | Value |
| --- | --- |
| `PANDECT_LOCAL_EMBEDDING_URL` | Private Ollama origin reachable by the application, e.g. `http://host.docker.internal:11434` in Docker Desktop. |
| `PANDECT_LOCAL_EMBEDDING_MODEL` | `all-minilm:22m` |
| `PANDECT_LOCAL_EMBEDDING_SHA256` | Full 64-character installed manifest digest. |
| `PANDECT_LOCAL_EMBEDDING_RUNTIME` | `ollama@<exact reviewed version>` |

The client validates the digest-addressed local model and its runtime. See
[index recovery](background-tasks.md#meaning-search-indexing) and the
[upgrade procedure](operations.md#switch-the-meaning-search-provider).

## Start optional CPU transcription

The ordinary stack needs no inference services. The `local-ai` Compose profile
adds only `local-transcription`; Ollama text processing uses the separately
managed service above. Startup/inference never downloads models. Obtain reviewed
[Whisper large-v3-turbo weights](https://huggingface.co/ggerganov/whisper.cpp/tree/main),
verify SHA-256 against upstream metadata and retain the source revision. Store
this file in ignored `data/models/transcription.bin`, or set
`PANDECT_LOCAL_TRANSCRIPTION_MODEL_FILE`; never put it under source-controlled
`inference/`.

For web/workers inside Compose, after [local setup](development.md#first-run)
creates `.env`, record the model digest and add a private runtime token without
printing it:

```sh
uv run --locked python - <<'PY'
import hashlib
import secrets
from pathlib import Path

with Path("data/models/transcription.bin").open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
values = {
    "PANDECT_LOCAL_TRANSCRIPTION_URL": "http://local-transcription:8080",
    "PANDECT_LOCAL_TRANSCRIPTION_SHA256": digest,
    "PANDECT_LOCAL_AI_TOKEN": secrets.token_urlsafe(32),
}
path = Path(".env")
lines = path.read_text().splitlines()
lines = [line for line in lines if line.split("=", 1)[0] not in values]
path.write_text("\n".join(lines + [f"{key}={value}" for key, value in values.items()]) + "\n")
path.chmod(0o600)
PY
docker compose --profile local-ai build local-transcription
docker compose --profile local-ai up -d --wait local-transcription
```

Use the configured path when hashing an alternative file. Rerunning rotates the
token; recreate web, worker and `local-transcription` with the same values. If an
Ollama proxy uses `PANDECT_LOCAL_AI_TOKEN`, update its authentication to match too.
No database/evidence is reset. Missing files/tokens or digest mismatches block
runtime startup. Unconfigured URLs produce recoverable analysis errors; case
management stays usable.

The transcription runtime verifies its read-only mounted model before launching
Whisper. Changes during verification are rejected; later changes/unavailability
stop the engine. Authenticated `/identity` reports model digest/engine revision.
Before sending evidence, Pandect compares these against the queued snapshot.
Each POST carries expected identity, checked under the runtime request lock before
content is read or forwarded. Replacing an engine under an alias cannot silently
change a run. Whisper listens only on container loopback; the front door exposes
only identity, readiness and transcription routes.

The internal `inference` network joins web/workers and transcription, with no
published ports or transcription runtime internet route. Host-based web/workers
need a separately reachable private transcription endpoint preserving this
identity/authentication contract. The container has a read-only filesystem,
private temporary memory, no evidence mount/external AI credentials/privileged
capabilities, and no payload logging. It accepts one request at a time. Deadlines
kill/reap the native process group; container restart reloads the verified model.
Cancellation withholds publication, but submitted requests may continue until
completion/deadline.

## Configuration and capacity

Set identical web/worker values. `*_URL` explicitly designates operator control;
a private hostname alone does not. Transcription requires the supplied front
door/identity contract; text requires the native Ollama API described above.
LAN services need TLS or a trusted encrypted tunnel and a firewall
restricting access to Pandect workers. Never expose runtimes to untrusted networks.

| Variable | Default / purpose |
| --- | --- |
| `PANDECT_LOCAL_TEXT_URL`, `PANDECT_LOCAL_TRANSCRIPTION_URL` | Empty disables the corresponding runtime; Compose examples are above. |
| `PANDECT_LOCAL_TEXT_MODEL`, `PANDECT_LOCAL_TRANSCRIPTION_MODEL` | `qwen3.5:9b`, `whisper-large-v3-turbo`; must match runtime identity. |
| `PANDECT_LOCAL_TEXT_SHA256`, `PANDECT_LOCAL_TRANSCRIPTION_SHA256` | Required installed manifest digest for Ollama text; exact model-file SHA-256 for Whisper. |
| `PANDECT_LOCAL_TEXT_RUNTIME`, `PANDECT_LOCAL_TRANSCRIPTION_RUNTIME` | Text defaults empty and requires `ollama@<exact reviewed server version>`; transcription defaults to the pinned Whisper revision above. Change deliberately with a reviewed runtime update. |
| `PANDECT_LOCAL_AI_TOKEN` | Required bearer token for bundled Whisper; optional proxy token for Ollama. Never retained in run parameters. |

Local inference uses a 600-second request deadline, 8 MiB request and 2 MiB
response limits, and a 4,096-token output allowance. Ollama text has a
16,384-token context, including the complete rendered prompt and output allowance.
These bounded defaults live in `config/settings.py`, rather than environment
variables. The bundled Whisper front door uses the same request/response limits
and deadline, with four native threads.

Compose limits Whisper to four CPUs and `4g` memory. These are resource ceilings,
not host minimum checks, and do not configure Ollama. For a measured hardware
requirement, apply a Compose override instead of adding environment variables:

```yaml
services:
  local-transcription:
    cpus: 8
    mem_limit: 8g
```

Save outside source control as `/tmp/pandect-capacity.yaml`, then run:

```sh
docker compose -f compose.yaml -f /tmp/pandect-capacity.yaml --profile local-ai up -d --wait local-transcription
```

Measure Ollama memory and latency with the chosen Qwen3.5 artifact, context and
concurrency alongside web, worker and PostgreSQL. Model weights alone do not
establish capacity; no hardware or throughput qualification is implied. Ollama's
own installation controls text hardware acceleration.

The reproducible Whisper image is CPU-only. For NVIDIA transcription, separately
pin an image retaining `server.py`/`revisions.json` and the native Whisper commit.
Build Whisper with `-DGGML_CUDA=ON`, matching CUDA libraries and host driver.
Preserve model/authentication/read-only/internal-network configuration. After
qualification, use an override such as:

```yaml
services:
  local-transcription:
    image: ${PANDECT_LOCAL_GPU_IMAGE:?Set a qualified image digest}
    gpus: all
    environment:
      PANDECT_RUNTIME_GPU: "true"
```

Save outside source control as `/tmp/pandect-gpu.yaml`, set
`PANDECT_LOCAL_GPU_IMAGE` to the reviewed digest, then run:

```sh
docker compose -f compose.yaml -f /tmp/pandect-gpu.yaml --profile local-ai up -d --no-build local-transcription
```

GPU controls enable offload only; they add no GPU support to the CPU image.
The CPU build qualifies no GPU driver/image or throughput.

Ollama text requests bound the full rendered prompt as described above and
reserve output tokens. Oversized selections fail with narrowing instructions,
never silent truncation; session chunks preserve messages. Inference runs outside
the deterministic parser's 1 GiB/20-second boundary.

New runs retain backend, artifact, runtime, settings, adapter version and location.
Request/interpretation changes require adapter-version changes, preserving history.
Queued model/endpoint or execution-setting drift fails recoverably; select new
settings in a new run. Updating application defaults can therefore require new
runs; earlier results and their recorded settings remain unchanged.
Private text retains adapter version `ollama-1`. Shared prompts and strict JSON
validation reject nonfinite numbers (including exponent overflow) and malformed
output. Queued older execution identities fail with `execution_changed` before
dispatch. External requests use separate `GatewayRequest` accounting and
require approval.

## Upgrade existing local text setups

Local text now uses Ollama only. The separate llama.cpp adapter, `local-text`
Compose service and bundled text-model build are removed. Provision/review the
local Qwen3.5 artifact, then configure its Ollama URL, manifest digest and exact
server-version pin using the procedure above. An empty text runtime pin disables
text inference until configured. Remove obsolete `PANDECT_LOCAL_TEXT_DRIVER`,
`PANDECT_LOCAL_TEXT_MODEL_FILE` and `PANDECT_LOCAL_TEXT_MEMORY` entries from the
operator configuration.

Restart web and worker together on the same release/settings. Queued llama.cpp
runs fail recoverably with `execution_changed`; start new Ollama runs after
configuration. Saved results, reviews and provenance remain intact. Runtime
replacement requires no database migration, evidence deletion or model-file
cleanup. Removing the Compose service definition does not stop an already
running old container; retire it explicitly during an operator-controlled update,
without removing retained model files or evidence.

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

## Transcription qualification

New automatic transcription uses the case's selected AI backend. Text follow-ups
use its usable transcript. Earlier batches with two queued transcribers keep
their recorded execution routes and use one candidate for text follow-ups.

To compare configured transcribers, create synthetic English recordings with
independently reviewed reference words and intervals. Include varied voices,
phone-quality sound, background noise, overlap, names, numbers, negation,
non-speech and speech crossing a 30-second boundary. Run each route in an
isolated synthetic case with the corresponding selection and approval. Score the
retained text against the same references with fixed normalization. Record paired
word error rates, critical-word errors, invented speech during non-speech clips,
timestamp errors and failed clips. Treat failed speech clips as omissions in the error
rate. Keep the private preference if the result is inconclusive.

Retain the benchmark and its nonsecret profile identities for later comparison.
`PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE` applies only to earlier queued
batches with a pair of transcription routes.

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

The command runs text methods through local adapters and citation validators
using fixed synthetic messages. It writes no database records and calls no external
AI service. Optional audio exercises PCM parsing and chunk/timeline handling. It
exits nonzero if a method has status `failed`. A returned result can have status
`completed` with a partial outcome, such as retained keywords after model failure.
Check each method's `output.outcome`, warnings, and suggestions against the
expected distinctions. The JSON also records usage and elapsed time. Keep audio
and output private.

1. Exercise each configured private and case-approved external path;
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

The historical Qwen3-8B/llama.cpp
[29 October trial](archive/local-inference-qualification-2026-10-29.md) found
unsupported specificity despite valid quotes and excluded question synthesis.
That removed text backend's trial qualifies no current Ollama model/runtime,
capacity or map service. Current models and GPU execution require separate
evaluation.

Correct configuration/capacity and start another run to recover. Never edit
retained runs, force-reset tasks or delete originals. Restart bundled transcription
with `docker compose --profile local-ai restart local-transcription`; manage
Ollama through its owning installation. Back up models/source/checksum metadata
separately from evidence; coordinate
PostgreSQL/private storage backups as described in [operations](operations.md).

Primary references: [Ollama chat API](https://docs.ollama.com/api/chat),
[Ollama 0.40.11 digest-addressed manifests](https://github.com/ollama/ollama/blob/v0.40.11/manifest/manifest.go),
[whisper.cpp server](https://github.com/ggml-org/whisper.cpp/blob/d1be6fde11ac6e0407606b4e42fe72d34add8037/examples/server/README.md),
[Whisper limitations](https://huggingface.co/openai/whisper-large-v3-turbo),
[Nominatim administration](https://nominatim.org/release-docs/latest/admin/Installation/)
and [Overpass setup](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html).
