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
3. Review account-level logging and retention for the approved destination. The application
   disables search caching, query rewriting, reranking and public endpoints when it creates
   an instance. Before each upload or search Pandect also checks both the namespace
   and instance public endpoint settings and rechecks the case grant. Its search API requests disable payload logging and caching in Gateway
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

Queries can run while indexing continues. Indexed passages become eligible individually
after Cloudflare reports completion. Upload acceptance and indexing completion are separate
states. Search applies the investigator's cutoff and type filters, reloads current membership
and owner approval, and checks candidate text against current source versions and offsets.
Each accepted match opens a current case record or finding. Case Chat uses the same locators
to verify quoted evidence. Metadata-only records and an unsupported match do not establish
a claim. Narrative relationship suggestions have a separate passage-linked review workflow.

## Recovery and withdrawal

Processing shows indexing state and completed passage counts. Owners and editors can resume
a failed active index. Search and upload usage records contain transferred byte counts and
status. A lost worker delivery retains its cursor and token. Inspect and recover a single
delivery from the application environment with:

```sh
python manage.py prepare_case_search --case CASE_UUID --status
python manage.py prepare_case_search --case CASE_UUID --retry
python manage.py prepare_case_search --case CASE_UUID --confirm-interrupted DELIVERY_TOKEN
```

Use the last command after confirming the worker stopped. A superseded delivery cannot
publish against a later case approval. Upload retries address the same immutable receipt
key. Unavailable responses retry after 15, 60 and 240 seconds and then require recovery.
Items that Cloudflare rejects remain visible as an indexing failure while other eligible
passages stay searchable.

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
