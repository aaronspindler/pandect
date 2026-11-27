# Private case search

Case Search, record filters and Evidence chat retrieve passages from a private
Cloudflare AI Search instance per enabled case. **Search evidence across cases**
on the case list queries up to eight accessible enabled cases per page and shows
up to four verified ranked passages per case. Open a case's search for up to
30 passages and record type and date filters. These results show ranked
retrieval, not a total match count. **Browse case evidence** opens the retained
collection. Clear the query to browse collections when hosted search is
unavailable.

## Configure the production namespace

1. Create a private AI Search namespace named `production`. Give tests separate
   credentials and a namespace with its public endpoint disabled.
2. On web and worker, set `PANDECT_CLOUDFLARE_SEARCH_TOKEN` to an account token
   with AI Search Edit and Run permissions. Set
   `PANDECT_CLOUDFLARE_ACCOUNT_ID` if it differs from `PANDECT_AI_ACCOUNT_ID`
   and `PANDECT_CLOUDFLARE_SEARCH_NAMESPACE` for a different namespace.
3. Review retention and logging. Pandect checks namespace and instance privacy
   and the current case grant before uploads and queries. Requests set Gateway
   headers to skip logging and caching. Service retention terms still apply.
4. The case owner enables **External AI** in Case settings. Pandect queues a
   private opaque instance and the eligible inventory. An unrelated model
   change keeps the index. A changed search destination or a new grant after
   withdrawal creates a replacement.

See the [instance API](https://developers.cloudflare.com/ai-search/api/instances/rest-api/),
[Items API](https://developers.cloudflare.com/ai-search/api/items/rest-api/),
[search API](https://developers.cloudflare.com/ai-search/api/search/rest-api/)
and [namespace endpoint settings](https://developers.cloudflare.com/ai-search/configuration/retrieval/public-endpoint/namespace/).

## Instance defaults

New instances use private built-in uploads. Hybrid retrieval combines native
Qwen3 Embedding 0.6B with Porter keyword matching using reciprocal rank
fusion. Receipts split into 256-token chunks with 10% overlap. The keyword
index matches all terms (AND), and the minimum vector similarity is 0.4.
Metadata filters narrow results without boosting recency or source type.

The instance default is ten results. Pandect generally asks for twice the
caller's limit, up to 50, to allow for rejected candidates. OCR, summaries,
query rewriting, similarity caching and reranking are off. Evidence chat
generates answers through its separately approved Agent and model. See
[chunking](https://developers.cloudflare.com/ai-search/configuration/indexing/chunking/),
[result controls](https://developers.cloudflare.com/ai-search/configuration/retrieval/result-controls/)
and [limits and pricing](https://developers.cloudflare.com/ai-search/platform/limits-pricing/).

### Applying the defaults to an existing index

Older indexes may retain OR keyword matching. Pandect uses the mode stored for
that index on each query, and index retries preserve current settings.

For an approved update, match the instance to the active local index, case
grant, account and namespace. Read back its settings and capture existing item
IDs. The embedding model is fixed at creation. Changes to tokenizer, index
method, OCR or file summaries require reindexing. Send the complete
`indexing_options` object when changing it.

Verify the new settings and privacy, then update the matching local index's
`keyword_match_mode` to `and` if applicable. Verify each captured item
reaches `completed` under the updated settings. For built-in uploads without a
confirmed reindex, call [item sync](https://developers.cloudflare.com/api/resources/ai_search/subresources/namespaces/subresources/instances/subresources/items/methods/sync/)
with `next_action=INDEX` and verify completion. After interruption, read
current settings and item states before resuming. See the
[instance update API](https://developers.cloudflare.com/api/resources/ai_search/subresources/namespaces/subresources/instances/methods/update/).

## Scope, indexing and citations

The index includes active case text, transcripts, source descriptions, file and
location metadata, extracted passages, eligible findings and reports, people,
vehicles, points of interest, reported social accounts and saved possible trips.
Legacy notes on person, vehicle and place records can be indexed. Dated
[Important](cases.md#important-items) notes aren't part of this scope. Binary
originals stay in Pandect during indexing. External analysis has separate
approved payloads.

Each UTF-8 receipt contains up to 2,048 Unicode characters from one source
version. Its filename is opaque. Typed metadata supplies record kind, surface,
availability time and event date interval. Filters use the observed or occurred
time, then collection or creation time as available. A trip uses its saved
interval. Pandect keeps locators, hashes and delivery state to verify the match
and reopen the exact original passage, attachment, record or finding.

Each case can upload four receipts at once, with privacy and approval checked
per upload. The worker can check 50 recent item statuses, up to ten exact
receipt filenames, and up to two additional items individually. Additional
[worker replicas](background-tasks.md#parallel-local-workers) reduce queue wait
for independent work. The per-case limit still applies.

Web and workers share a PostgreSQL request budget of 3.5 requests per second
per Cloudflare account, with a burst of 24. HTTP 429 shares a cooldown using
`Retry-After` or five minutes. See
[Cloudflare API rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/).

Passages become searchable individually after indexing completes. Queries
recheck membership, owner approval, the cutoff and exact source version and
offset. Evidence chat uses the same locators to verify quotations. Narrative
relationship suggestions have a separate passage-linked review workflow.

## Recovery and withdrawal

In **Processing → Cloudflare AI Search indexing**, see preparation, upload,
indexing and failures. Current sources can produce several passages, so the
passage count can grow during preparation. The panel shows the last available
Cloudflare status and identifies previously indexed passages under recheck.

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
cooldown ends. Workers resume local budget deferrals at the next eligible time
without using a retry attempt.
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
