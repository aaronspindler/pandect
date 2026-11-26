from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import transaction
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.case_search import cloudflare, hosted, lifecycle, material
from core.case_search.discovery import discover
from core.case_search.indexing import prepare_case_search
from core.case_search.tests.fixtures import CONFIG, indexed
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource
from core.testing import EvidenceViewTestMixin
from geo.models import PointOfInterest
from socialmedia.models import SocialAccount
from text.models import TextEvidence


@override_settings(PANDECT_CLOUDFLARE_SEARCH=CONFIG)
class HostedCaseSearchTests(EvidenceViewTestMixin, TestCase):
    def test_person_note_edit_only_updates_the_entity_record(self):
        with patch("core.case_search.index_hooks.reconcile") as refresh:
            self.person.notes = "New private note"
            self.person.save()
            refresh.assert_not_called()
            self.person.first_name = "New reported name"
            self.person.save()
            refresh.assert_called_once_with(self.case.pk)

    @patch("core.case_search.cloudflare.request")
    def test_date_filter_precedes_provider_rank_limit_and_is_rechecked(self, request):
        self.setup_item()
        day = self.item.event_start
        after, before = day - timedelta(hours=1), day + timedelta(hours=1)
        self.assertEqual(self.item.event_end, day)
        # Fifty-one higher-ranked, out-of-period items would consume the entire page.
        ranked = [
            (
                day - timedelta(days=2),
                {"item": {"key": uuid4().hex + ".txt"}, "text": "café"},
            )
            for _ in range(51)
        ] + [(day, self.chunk)]

        def lookup(*args, payload, **kwargs):
            retrieval = payload["ai_search_options"]["retrieval"]
            filters = retrieval["filters"]
            lower = filters.get("to_at", {}).get("$gte", float("-inf"))
            upper = filters.get("from_at", {}).get("$lt", float("inf"))
            self.assertEqual((lower, upper), (after.timestamp(), before.timestamp()))
            eligible = [
                hit for date, hit in ranked if lower <= date.timestamp() < upper
            ]
            return {"chunks": eligible[: retrieval["max_num_results"]]}

        request.side_effect = lookup
        date_scope = {"start": after, "end": before}
        self.assertTrue(
            hosted.search(
                self.case, self.users["viewer"], "café", filters=date_scope, limit=30
            )
        )
        self.original.collected_at = day - timedelta(days=4)
        self.original.save(update_fields=["collected_at"])
        self.assertEqual(
            hosted.search(self.case, self.users["viewer"], "café", filters=date_scope),
            [],
        )

    @patch("core.case_search.cloudflare.exchange")
    def test_privacy_drift_blocks_query_payload_and_usage_excludes_query_text(
        self, exchange
    ):
        self.setup_item()
        namespace = {"name": self.index.namespace, "public_endpoint_params": None}
        instance = {
            "id": self.index.instance,
            "public_endpoint_params": {"enabled": True},
        }
        exchange.side_effect = [(namespace, 50), (instance, 50)]
        with self.assertRaisesMessage(
            hosted.HostedSearchError, "search_instance_private"
        ):
            hosted.search(self.case, self.users["owner"], "synthetic question")
        self.assertEqual(
            [call.args[0].get_method() for call in exchange.call_args_list],
            ["GET", "GET"],
        )
        instance["public_endpoint_params"]["enabled"] = False
        exchange.side_effect = [(namespace, 50), (instance, 50), (self.response, 75)]
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(
                hosted.search(self.case, self.users["owner"], "synthetic question")
            )
        from core.models import GatewayRequest

        usage = GatewayRequest.objects.get(case=self.case, operation="search_query")
        self.assertEqual(usage.response_bytes, 75)
        self.assertGreater(usage.request_bytes, 0)
        self.assertNotIn("synthetic question", str(usage.__dict__))

    def test_reported_accounts_and_poi_keep_exact_case_record_links(self):
        account = SocialAccount.objects.create(
            person=self.person, platform="Example", username="synthetic_handle"
        )
        poi = PointOfInterest.objects.create(
            person=self.person,
            created_by=self.users["owner"],
            latitude=12,
            longitude=34,
        )
        sources = [("social", "person", account.pk), ("record", "poi", poi.pk)]
        with indexed(self.case, sources):
            actor = self.users["viewer"]
            records = discover(self.case, actor, "synthetic_handle", records=True)
            self.assertEqual(records.records["person"], {str(self.person.pk)})
            found = hosted.hits(self.case, actor, "synthetic_handle", records=True)[0][
                0
            ]
            quote = material.corpus.cite(
                self.case,
                found.locator,
                found.text.index(account.username),
                account.username,
            )
            self.assertIn(str(self.person.pk), quote["url"])
            poi_matches = discover(self.case, actor, "Synthetic person", records=True)
            self.assertEqual(poi_matches.records["poi"], {str(poi.pk)})
            self.person.first_name = "Renamed person"
            self.person.save()
            self.assertEqual(hosted.search(self.case, actor, "Synthetic person"), [])
            self.assertTrue(HostedSearchSource.objects.get(source_key=poi.pk).dirty)
            account.username = "new_handle"
            account.save()
            self.assertTrue(HostedSearchSource.objects.get(source_key=account.pk).dirty)
            self.assertIsNone(
                material.corpus.cite(
                    self.case, found.locator, quote["start"], quote["quote"]
                )
            )

    @patch("core.case_search.cloudflare.request")
    def test_public_namespace_stops_before_provision_or_transfer(self, request):
        index = self.toggle()
        request.return_value = {
            "name": index.namespace,
            "public_endpoint_params": {"enabled": True},
        }
        lifecycle.sync_index.call(str(index.pk), str(index.token))
        index.refresh_from_db()
        self.assertFalse(index.provisioned)
        self.assertEqual(index.error_code, "search_instance_private")
        self.assertEqual(request.call_count, 1)

    @patch("core.case_search.cloudflare.request")
    def test_existing_instance_and_create_conflict_preserve_instance_settings(
        self, request
    ):
        index = self.toggle()
        HostedSearchIndex.objects.filter(pk=index.pk).update(keyword_match_mode="or")
        index.refresh_from_db()
        namespace = {"name": index.namespace}
        existing = {
            "id": index.instance,
            "public_endpoint_params": {"enabled": False},
            "chunk_size": 1024,
            "reranking": True,
        }
        for replies, methods in (
            ([namespace, existing], ["GET", "GET"]),
            ([namespace, None, None, existing], ["GET", "GET", "POST", "GET"]),
        ):
            with self.subTest(methods=methods):
                request.reset_mock()
                request.side_effect = replies
                cloudflare.ensure_instance(index, lambda: None)
                self.assertEqual([c.args[1] for c in request.call_args_list], methods)
                if "POST" in methods:
                    payload = request.call_args_list[2].kwargs["payload"]
                    self.assertEqual(
                        payload["retrieval_options"]["keyword_match_mode"], "or"
                    )

    @patch("core.case_search.cloudflare.request")
    def test_retired_index_keeps_cleanup_receipt_until_absence_confirmed(self, request):
        index = self.toggle()
        self.toggle(False)
        index.refresh_from_db()
        request.side_effect = [None, {"id": index.instance}]
        lifecycle.sync_index.call(str(index.pk), str(index.token))
        index.refresh_from_db()
        self.assertEqual(index.error_code, "search_cleanup_pending")
        request.side_effect = [None, None]
        lifecycle.sync_index.call(str(index.pk), str(index.token))
        self.assertFalse(HostedSearchIndex.objects.filter(pk=index.pk).exists())

    def toggle(self, enabled=True, case=None):
        case = case or self.case
        case.external_ai_enabled = enabled
        case.external_ai_revision = uuid4()
        case.external_analysis_approval = (
            {"hosted_search": cloudflare.destination()} if enabled else {}
        )
        with transaction.atomic():
            case.save()
            hosted.case_setting_changed(case)
        return HostedSearchIndex.objects.filter(case_key=case.pk, retired=False).first()

    def setup_item(self):
        self.index = self.toggle()
        self.index.provisioned = True
        self.index.save()
        self.original = self.text()
        source = HostedSearchSource.objects.get(
            index=self.index, source_key=self.original.pk
        )
        lifecycle.prepare_window(self.case, source)
        self.item = HostedSearchItem.objects.get(source=source)
        self.item.status = "completed"
        self.item.save()
        self.chunk = {"item": {"key": self.item.filename}, "text": "café"}
        self.response = {"chunks": [self.chunk]}

    @patch("core.case_search.cloudflare.request")
    def test_search_reopens_exact_source_span_after_case_access_check(self, request):
        self.setup_item()
        request.return_value = self.response
        for role in ("owner", "editor", "viewer"):
            hits = hosted.search(self.case, self.users[role], "harbour")
            locator, pos = hits[0]
            self.assertEqual(locator["pk"], str(self.original.pk))
            self.assertEqual(
                material.corpus.cite(self.case, locator, pos, "café")["quote"], "café"
            )
        self.assertEqual(request.call_args.args[2], "/search")
        options = request.call_args.kwargs["payload"]["ai_search_options"]
        retrieval = options["retrieval"]
        self.assertEqual(retrieval["retrieval_type"], "hybrid")
        self.assertEqual(retrieval["match_threshold"], 0.4)
        self.assertEqual(retrieval["keyword_match_mode"], "and")
        self.assertEqual(retrieval["max_num_results"], 16)
        for option in ("cache", "query_rewrite", "reranking"):
            self.assertIs(options[option]["enabled"], False)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        with self.assertRaises(Http404):
            hosted.search(self.case, self.users["viewer"], "harbour")
        self.assertEqual(request.call_count, 3)

    @patch("core.case_search.cloudflare.request")
    def test_legacy_index_keeps_its_query_mode(self, request):
        self.setup_item()
        HostedSearchIndex.objects.filter(pk=self.index.pk).update(
            keyword_match_mode="or"
        )
        request.return_value = self.response
        self.assertTrue(hosted.search(self.case, self.users["viewer"], "harbour notes"))
        options = request.call_args.kwargs["payload"]["ai_search_options"]
        self.assertEqual(options["retrieval"]["keyword_match_mode"], "or")

    @patch("core.case_search.cloudflare.request")
    def test_wrong_case_unverified_text_cutoff_and_archived_receipts(self, request):
        self.setup_item()
        other = self.toggle(case=self.other_case)
        source = HostedSearchSource.objects.create(
            index=other, surface="original", kind="text", source_key=self.original.pk
        )
        foreign = HostedSearchItem.objects.create(
            source=source,
            version=self.item.version,
            start=0,
            length=3,
            digest=self.item.digest,
            available_at=self.item.available_at,
            event_start=self.item.event_start,
            event_end=self.item.event_end,
            status="completed",
        )
        request.return_value = {
            "chunks": [
                {"item": {"key": foreign.filename}, "text": "café"},
                self.chunk | {"text": "fabrication"},
            ]
        }
        self.assertEqual(hosted.search(self.case, self.users["owner"], "file"), [])
        request.return_value = self.response
        self.assertEqual(
            hosted.search(
                self.case,
                self.users["owner"],
                "file",
                through=self.item.available_at - timedelta(seconds=1),
            ),
            [],
        )
        TextEvidence.objects.filter(pk=self.original.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.assertEqual(hosted.search(self.case, self.users["owner"], "file"), [])

    @patch("core.case_search.cloudflare.request")
    def test_disable_and_destination_change_block_queries_and_quick_matches(
        self, request
    ):
        self.setup_item()
        self.case.external_analysis_approval["hosted_search"]["namespace"] = "changed"
        self.case.save()
        with self.assertRaisesMessage(hosted.HostedSearchError, "search_configuration"):
            hosted.search(self.case, self.users["owner"], "file")
        self.toggle(False)
        self.login("viewer")
        url = reverse("core:search:suggestions", args=(self.case.pk,))
        self.assertEqual(self.client.get(url, {"q": "file"}).json(), {"results": []})
        request.assert_not_called()

    @patch("core.case_search.cloudflare.request")
    def test_revocation_during_search_discards_response(self, request):
        self.setup_item()

        def revoke(*args, **kwargs):
            self.toggle(False)
            return self.response

        request.side_effect = revoke
        with self.assertRaisesMessage(hosted.HostedSearchError, "search_disabled"):
            hosted.search(self.case, self.users["editor"], "file")

    @patch("core.case_search.cloudflare.request")
    def test_disable_reenable_and_whole_case_deletion_keep_cleanup_destination(
        self, request
    ):
        request.return_value = None
        old = self.toggle()
        original_token = str(old.token)
        self.toggle(False)
        current = self.toggle()
        self.assertNotEqual(current.instance, old.instance)
        self.assertEqual(
            lifecycle.sync_index.call(str(old.pk), original_token), "superseded"
        )
        old.refresh_from_db()
        lifecycle.sync_index.call(str(old.pk), str(old.token))
        self.assertEqual(request.call_args.args[0].revision, old.revision)
        self.assertEqual([c.args[1] for c in request.call_args_list], ["DELETE", "GET"])
        self.assertTrue(
            HostedSearchIndex.objects.filter(pk=current.pk, retired=False).exists()
        )
        self.case.delete()
        current.refresh_from_db()
        self.assertTrue(current.retired)
        lifecycle.sync_index.call(str(current.pk), str(current.token))
        self.assertFalse(HostedSearchIndex.objects.filter(pk=current.pk).exists())

    @patch("core.case_search.cloudflare.request")
    def test_provision_backfill_publish_and_retry_are_idempotent(self, request):
        original = self.text()
        index = self.toggle()
        receipts = {}

        def send(current, method, path="", **kwargs):
            self.assertEqual(current.pk, index.pk)
            if kwargs.get("namespace"):
                return {"name": index.namespace}
            if method == "GET" and not path:
                return None
            if kwargs.get("collection"):
                payload = kwargs["payload"]
                self.assertEqual(payload["id"], index.instance)
                self.assertNotIn("source", payload)
                self.assertNotIn("type", payload)
                self.assertEqual(payload["public_endpoint_params"], {"enabled": False})
                self.assertEqual(
                    payload["embedding_model"], "@cf/qwen/qwen3-embedding-0.6b"
                )
                self.assertEqual(
                    payload["index_method"], {"vector": True, "keyword": True}
                )
                self.assertEqual(
                    payload["indexing_options"],
                    {
                        "keyword_tokenizer": "porter",
                        "file_summary": False,
                        "use_ocr": False,
                    },
                )
                self.assertIs(payload["chunk"], True)
                self.assertEqual(
                    (payload["chunk_size"], payload["chunk_overlap"]), (256, 10)
                )
                self.assertEqual(payload["fusion_method"], "rrf")
                self.assertEqual(
                    payload["retrieval_options"],
                    {"keyword_match_mode": "and", "boost_by": []},
                )
                self.assertEqual(
                    (payload["score_threshold"], payload["max_num_results"]), (0.4, 10)
                )
                for option in ("cache", "rewrite_query", "reranking"):
                    self.assertIs(payload[option], False)
                return {
                    "id": current.instance,
                    "public_endpoint_params": {"enabled": False},
                }
            if path == "/items":
                key, text, metadata = kwargs["file"]
                self.assertLessEqual(
                    float(metadata["from_at"]), float(metadata["to_at"])
                )
                receipts[key] = text
                return {"key": key, "id": key[:-4], "status": "completed"}
            self.fail((method, path))

        request.side_effect = hosted.HostedSearchError("search_unavailable")
        for _ in range(4):
            lifecycle.sync_index.call(str(index.pk), str(index.token))
            index.refresh_from_db()
        self.assertEqual(index.status, "failed")
        self.assertTrue(prepare_case_search(self.case, self.users["owner"], retry=True))
        request.side_effect = send
        for _ in range(100):
            index.refresh_from_db()
            if index.status == "ready":
                break
            self.assertEqual(index.status, "queued")
            lifecycle.sync_index.call(str(index.pk), str(index.token))
        self.assertEqual(index.status, "ready")
        item = HostedSearchItem.objects.get(source__source_key=original.pk)
        self.assertIn(original.body, receipts[item.filename])
        self.assertEqual(item.status, "completed")
        lifecycle.changed(self.case.pk, "original", "text", [original.pk])
        src = HostedSearchSource.objects.get(pk=item.source_id)
        lifecycle.prepare_window(self.case, src)
        self.assertEqual(HostedSearchItem.objects.filter(source=src).count(), 1)

    @patch("core.case_search.cloudflare.request")
    def test_global_search_uses_only_current_memberships_and_owner_approvals(
        self, request
    ):
        self.setup_item()
        self.toggle(case=self.other_case)
        request.return_value = self.response
        self.login("editor")
        response = self.client.get(reverse("cases:search_evidence"), {"q": "notes"})
        self.assertIn(
            self.original.title, response.context["groups"][0]["rows"][0]["title"]
        )
        self.assertContains(response, "<mark>notes</mark>")
        self.assertNotContains(response, self.other_case.name)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0].case_key, self.case.pk)

    def test_deletion_manifest_includes_removal_receipt_and_keeps_work_on_deletion(
        self,
    ):
        from core.evidence.deletion import plan_deletion

        self.setup_item()
        plan = plan_deletion(self.original, "text")
        self.assertIn(
            str(self.item.pk),
            [r["id"] for r in plan.entries if r["action"] == "Queue hosted removal"],
        )
        self.original.delete()
        source = HostedSearchSource.objects.get(pk=self.item.source_id)
        self.assertTrue(source.dirty)
        lifecycle.prepare_window(self.case, source)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "delete")

    @patch("core.case_search.cloudflare.request")
    def test_partial_index_and_same_destination_revision_rotation(self, request):
        self.setup_item()
        before = self.index.token
        current = self.toggle()
        self.assertEqual(current.pk, self.index.pk)
        self.assertNotEqual(current.token, before)
        request.return_value = self.response
        self.assertTrue(hosted.search(self.case, self.users["owner"], "notes"))
        self.item.status = "poll"
        self.item.save()
        with self.assertRaisesMessage(hosted.HostedSearchError, "search_preparing"):
            hosted.search(self.case, self.users["owner"], "notes")
        self.assertEqual(request.call_count, 1)
