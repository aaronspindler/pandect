# Local inference qualification — 29 October 2026

This is a frozen report of the dated trial, not qualification of the current
checkout, runtime versions, or deployment. Use the [local inference guide](../local-analysis.md)
for current setup, qualification commands, and recovery. The local artifacts
named below are not included in the repository.

The four text paths and transcription listed below completed against the real
pinned engines and verified model files. This historical qualification predates
question synthesis and does not qualify that method. Forward/reverse geocoding
and geographic context also completed
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

The 8B model remains unqualified for dependable interpretation or semantic linking.
Reject unsupported specificity even when quotations validate. Different models and
GPU execution require separate evaluation.

At review time, raw synthetic results were retained locally in ignored
`data/validation/local-analysis/2026-10-29-qualification.json`; verified model
files and source/checksum metadata are in ignored `data/models/`. The initial
multi-method run lost runtime availability during an interrupted session;
separate completed trials supersede those interrupted method results.
These inference trials do not establish map coverage, replication freshness, or
historical data availability. Qualify those against the configured map services.
