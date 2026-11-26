# Private case search

Case Search, quick matches and Case Chat use Cloudflare AI Search for passage discovery.
Pandect owns originals, published analysis, investigator review and evidence links.
Cloudflare owns text and vector indexes in its built-in storage. A query searches one private
instance. **Search evidence across cases** on the case list searches up to eight accessible,
enabled cases per page. Each case has its own request and result group.

## Configure the production namespace

1. Create one AI Search namespace named `production` in the Cloudflare account. Keep the
   namespace public endpoint disabled. Use a different namespace and credentials for tests.
2. Configure the same server-side environment on the Django web process and the database worker.
   Set `PANDECT_CLOUDFLARE_ACCOUNT_ID` to the Cloudflare account ID (the existing
   `PANDECT_AI_ACCOUNT_ID` supplies the default). Set `PANDECT_CLOUDFLARE_SEARCH_TOKEN` to an
   account-scoped token with AI Search Edit and Run permissions. Set
   `PANDECT_CLOUDFLARE_SEARCH_NAMESPACE` when using a namespace other than `production`.
3. Review account-level logging and retention for the approved destination. Before each upload
   or search Pandect checks both the namespace and instance public endpoint settings and
   rechecks the case grant. Its search API requests disable payload logging and caching in Gateway
   headers. Cloudflare's service retention terms still apply to uploaded items.
4. The owner enables **External AI** in Case settings. Pandect records the account and
   namespace and queues creation of an opaque private instance. Existing cases start with
   external AI disabled. Enabling it queues the eligible case inventory for upload.
   Changing unrelated model grants keeps the same index. Changing the search destination or
   re-enabling after withdrawal creates a new instance.

The [Cloudflare instance API](https://developers.cloudflare.com/ai-search/api/instances/rest-api/)
documents private instance creation. The [Items API](https://developers.cloudflare.com/ai-search/api/items/rest-api/)
and [search API](https://developers.cloudflare.com/ai-search/api/search/rest-api/) describe indexing
and query permissions. Leave the production namespace out of
[public endpoint configurations](https://developers.cloudflare.com/ai-search/configuration/retrieval/public-endpoint/namespace/).

People, Vehicles, Evidence, Images and Findings show hosted search availability
in their filtered results. Clear the query to browse the current collection.

## Instance defaults

The worker sends the following settings when provisioning a new case index.

| Setting | Pandect default and reason |
| --- | --- |
| Data source | Private, built-in upload storage. Pandect sends text receipts directly. External connectors, crawl filters, service tokens and scheduled syncs have no role in this path. |
| Indexes and model | Native Qwen3 Embedding 0.6B for semantic matches and a Porter keyword index for names and identifiers. Hybrid retrieval merges both rankings with reciprocal rank fusion. |
| Chunking | 256 tokens with 10% overlap within a receipt. Short passages help Case Chat read around the match, including matches late in a long receipt. Overlap preserves phrases across chunk boundaries and adds ingestion tokens only to receipts that split. |
| Ranking | The keyword index looks for all query terms (AND). The vector index also retrieves paraphrases. The minimum vector similarity is 0.4. Metadata filters narrow the candidate set and no recency or source-type boost biases the ranking. |
| Results | The instance default is 10. Each Pandect search requests twice the number of results its caller needs, up to 50, to allow for candidates rejected during citation checks. |
| Optional processing | OCR and per-file summaries are off for the text receipts. Similarity caching and query rewriting are off to keep each query literal and current. Reranking is off to avoid another model pass on interactive searches. |
| Generation and access | Pandect calls the retrieval endpoint. Case Chat generates answers through its separately approved Agent route. The search instance needs no external model, custom prompt or public endpoint. Cache lifetime, public domains, public MCP, snippets and endpoint rate limits do not apply. |

Cloudflare includes native embedding and reranking in AI Search usage. Hybrid has
the same query price as vector search. Overlapping tokens count toward ingestion.
Rewriting and per-file summaries can incur separate model charges.
See [limits and pricing](https://developers.cloudflare.com/ai-search/platform/limits-pricing/),
[chunking](https://developers.cloudflare.com/ai-search/configuration/indexing/chunking/)
and [result controls](https://developers.cloudflare.com/ai-search/configuration/retrieval/result-controls/).

### Applying the defaults to an existing index

The schema migration preserves the previous OR keyword match mode for existing
index records. Pandect sends the mode saved for that index on every query. A retry
reuses the same index and checks privacy without overwriting its settings.

Before an operator-approved update, match the exact instance to its active local
index, current case grant, account and private namespace. Read its instance
configuration and indexing statistics. Compare them with the new defaults above.
Changes to the tokenizer, index method, OCR or file summaries require a full
reindex. For built-in upload storage, capture the existing item IDs before the update
so new arrivals can be tracked separately. The embedding model is fixed at creation.

After approval, update only the reviewed fields on that instance. Send the complete
`indexing_options` object when changing it. Read the configuration back, verify
privacy, then set that same active local index's `keyword_match_mode` to `and` in
the migrated application. Confirm that previously completed items actually enter
processing under the new settings. Built-in uploads have no scheduled sync job.
For items without a confirmed reindex, use the
[item sync API](https://developers.cloudflare.com/api/resources/ai_search/subresources/namespaces/subresources/instances/subresources/items/methods/sync/)
with `next_action` set to `INDEX`. Record each acknowledgement and verify every
retained item in the captured set reaches `completed`. If interrupted, read the
current settings and item states before resuming. See the
[Cloudflare configuration reference](https://developers.cloudflare.com/ai-search/configuration/)
and [instance update API](https://developers.cloudflare.com/api/resources/ai_search/subresources/namespaces/subresources/instances/methods/update/).

## Scope, indexing and citations

The worker reads active text, communication records and transcripts, file and location
metadata, retained extracted passages, published findings and analysis reports. People,
vehicles, points of interest and saved possible trips also contribute searchable records.
These include investigator notes, reported social account names and sources, and text
entered in the source description.
Original binary files stay in Pandect during search indexing. External model analysis has its
own approved payloads. Each uploaded UTF-8 file covers up to 2,048 Unicode characters of
one source version. Its filename contains an opaque receipt ID. Typed metadata includes
the source kind, surface, availability time and event date interval. The date filter uses the
source's observed or occurred time, then collection or creation time as available. A possible
trip spans its saved start and end. Cloudflare filters before ranking. Local receipts hold offsets, lengths,
hashes and delivery state. They allow Pandect to reopen the canonical source.

The worker prepares passages and checks their indexing status in bounded batches.
It can upload four passages at a time for a case. Each upload sends one receipt
after its own privacy and approval checks. A status check can read up to 50 items
on a Cloudflare list page and check up to two missing, due items individually.
Additional worker replicas can reduce queue wait for independent cases and jobs.
The per-case upload limit still applies.
See [parallel local workers](background-tasks.md#parallel-local-workers).

Web processes and workers share a PostgreSQL-backed request budget per Cloudflare
account for AI Search REST calls. The budget allows three requests per second
with a burst of up to 24 requests. If Cloudflare returns HTTP 429, Pandect shares
the cooldown across those processes. The cooldown uses a valid `Retry-After`
header or defaults to five minutes.
See [Cloudflare API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/).

Queries can run while indexing continues. Indexed passages become eligible individually
after Cloudflare reports completion. Upload acceptance and indexing completion are separate
states. Search applies the investigator's cutoff and type filters, reloads current membership
and owner approval, and checks candidate text against current source versions and offsets.
Each accepted match opens a current case record or finding. Case Chat uses the same locators
to verify quoted evidence. Metadata-only records and an unsupported match do not establish
a claim. Narrative relationship suggestions have a separate passage-linked review workflow.

## Recovery and withdrawal

In **Processing**, open **Cloudflare AI Search indexing** to see the current stage,
passages awaiting upload, passages indexing at Cloudflare, and indexed or failed passages.
The source total covers the case's current discovery scope, including sources the
worker has yet to prepare. A source can produce several passages, and preparation
checks whether each source can be indexed.
The panel also shows the last Cloudflare status recorded by the worker, when available.
Previously indexed passages being rechecked are included in **Indexed** and called out
separately. Passage counts cover prepared passages and can grow during preparation.
The panel refreshes as the worker progresses.

Owners and editors can resume a failed active index. Search and upload usage records
contain transferred byte counts and status. A lost worker delivery retains its cursor
and token. Inspect and recover a single
delivery from the application environment with:

```sh
python manage.py prepare_case_search --case CASE_UUID --status
python manage.py prepare_case_search --case CASE_UUID --retry
python manage.py prepare_case_search --case CASE_UUID --confirm-interrupted DELIVERY_TOKEN
```

Use the last command after confirming the worker stopped. A superseded delivery cannot
publish against a later case approval. Upload retries address the same immutable receipt
key. Unavailable responses retry after 15, 60 and 240 seconds and then require
recovery. HTTP 429 uses the same retry limit and waits at least until the shared
cooldown ends. Waiting for the shared request budget uses no retry attempts.
Items that Cloudflare rejects remain visible as an indexing failure while other
eligible passages stay searchable.

Disabling external AI immediately prevents further evidence queries and uploads. A durable
cleanup delivery deletes the captured instance. Whole-case deletion retains the same cleanup
receipt without source text. A new enablement uses a different instance, so a delayed
deletion cannot remove the replacement. If confirmation of creation was lost, cleanup
still issues deletion for the original opaque name. A 404 confirms absence. For an interrupted
or failed cleanup, run:

```sh
python manage.py prepare_case_search --cleanup --case CASE_UUID
```

Omit `--case` to resume all pending private instance deletions, including deleted cases.
Retain credentials for the original account until cleanup succeeds. If the installation
has moved accounts, run cleanup with credentials configured for the original account.
Search receipts and outbox rows are part of the PostgreSQL backup. Restore the current
approval state before resuming workers. Use the case recovery command to reconcile current
originals after a restore.
