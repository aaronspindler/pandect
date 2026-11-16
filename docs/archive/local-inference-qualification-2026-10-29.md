# Local inference qualification — 29 October 2026

Historical trial only; it does not qualify current code, runtimes, or deployment.
The [local inference guide](../local-analysis.md) owns current procedures. Named
local artifacts are outside the repository.

Four text paths and transcription completed with real pinned engines and
verified models; question synthesis was not yet implemented or qualified.
Forward/reverse geocoding and geographic context used synthetic private HTTP,
real geometry parsing, and a validated retained reference. Docker networking was
internal; external TCP failed. No investigation records or Gateway requests
were created.

| Method | Observed result | Elapsed time |
| --- | --- | --- |
| Topics/short summaries | Six model suggestions had valid quotations, but an ambiguous place became a specific venue and labels repeated source types. Interpretation remains unqualified. | 314 s |
| Conversation summaries | Five message summaries had valid quotations and preserved cancellation/hearsay; an ambiguous venue was incorrectly resolved. Interpretation remains unqualified. | 174 s |
| Claims/events | Six quoted suggestions preserved ambiguity and qualifications, but some event/relationship classifications were weak. They still require review. | 173 s |
| Semantic conversation links | All six model suggestions cited only one source. The guard retained them as unresolved; no completed two-sided model link was demonstrated. | 193 s |
| Transcription | The words in a 3.565-second generated English recording matched the supplied script. Two source-timed segments and two unresolved gaps produced a partial result. This is not a general recognition-quality measurement. | 20.6 s |
| Geocoding | Forward and reverse HTTP lookups returned the synthetic candidate without external approval. | Not benchmarked |
| Geographic context | Private HTTP lookup, bounded parsing and the retained reference passed against the synthetic map response. | Not benchmarked |

CPU-only Linux/arm64 runtimes each had four CPUs on a 16 GiB host with a ~7.75 GiB
Docker VM. Text used **4,096 context / 1,024 output tokens**, below defaults.
Native peak RSS was ~**5.96 GiB text / 1.70 GiB transcription**. The first topic
trial overlapped builds/tests; later text trials used a warm runtime. Serial
trials did not qualify combined-service memory, production throughput, maximum
context, accuracy rates, or remote-model parity.

The 8B model remains unqualified for dependable interpretation or semantic linking.
Reject unsupported specificity even when quotations validate. Different models and
GPU execution require separate evaluation.

Raw synthetic results were retained in ignored
`data/validation/local-analysis/2026-10-29-qualification.json`; verified models
and source/checksum metadata in ignored `data/models/`. Completed separate trials
superseded an interrupted multi-method run that lost runtime availability.
Qualify map coverage, replication freshness, and historical availability against
configured map services; these trials did not establish them.
