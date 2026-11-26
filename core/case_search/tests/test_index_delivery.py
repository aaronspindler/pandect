from collections import defaultdict
from datetime import timedelta
from threading import Barrier, Lock, get_ident
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from cases.models import Case
from core.case_search import cloudflare, lifecycle, material
from core.case_search.tests.fixtures import CONFIG
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource
from text.models import TextEvidence


class HostedIndexDeliveryTests(TransactionTestCase):
    def setUp(self):
        config = CONFIG | {"account": uuid4().hex}
        self.enterContext(self.settings(PANDECT_CLOUDFLARE_SEARCH=config))
        self.owner = get_user_model().objects.create_user(
            username="index-delivery-owner", email="index-owner@example.test"
        )
        EmailAddress.objects.create(
            user=self.owner, email=self.owner.email, verified=True, primary=True
        )
        self.case = Case.objects.create(
            owner=self.owner,
            name="Synthetic indexing case",
            external_ai_enabled=True,
            external_analysis_approval={"hosted_search": cloudflare.destination()},
        )
        self.index = HostedSearchIndex.objects.create(
            case_key=self.case.pk,
            revision=self.case.external_ai_revision,
            account=config["account"],
            namespace=config["namespace"],
            provisioned=True,
            phase=len(material.surfaces()),
        )
        self.items = HostedSearchItem.objects.filter(source__index=self.index)

    def create_sources(self, count):
        return [
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic source {number}",
                body=f"Retained evidence text {number}",
            )
            for number in range(count)
        ]

    def deliver(self):
        lifecycle.sync_index.call(str(self.index.pk), str(self.index.token))
        self.index.refresh_from_db()

    def stage_passages(self, count):
        self.create_sources(count)
        for source in HostedSearchSource.objects.filter(index=self.index):
            lifecycle.prepare_window(self.case, source)
        return list(self.items.order_by("id"))

    @staticmethod
    def response(item, status="completed"):
        return {"key": item.filename, "id": item.remote_id, "status": status}

    def test_delivery_prepares_passages_and_overlaps_four_private_uploads(self):
        self.create_sources(5)
        simultaneous = Barrier(4)
        lock = Lock()
        checks = defaultdict(list)
        sent, in_flight, peak = 0, 0, 0
        namespace_path = f"/ai-search/namespaces/{self.index.namespace}"
        instance_path = namespace_path + f"/instances/{self.index.instance}"

        def exchange(request, *_):
            nonlocal sent, in_flight, peak
            path = urlsplit(request.full_url).path
            thread = get_ident()
            if path.endswith(namespace_path):
                checks[thread].append("namespace")
                return {"name": self.index.namespace}, 50
            if path.endswith(instance_path):
                checks[thread].append("instance")
                return {
                    "id": self.index.instance,
                    "public_endpoint_params": {"enabled": False},
                }, 50
            self.assertTrue(path.endswith(instance_path + "/items"))
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(checks.pop(thread), ["namespace", "instance"])
            filename = request.data.split(b'filename="', 1)[1].split(b'"', 1)[0]
            key = filename.decode()
            with lock:
                sent += 1
                first_batch = sent <= 4
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                if first_batch:
                    simultaneous.wait(timeout=10)
                return {"key": key, "id": key[:-4], "status": "completed"}, 50
            finally:
                with lock:
                    in_flight -= 1

        with patch.object(cloudflare, "exchange", side_effect=exchange):
            self.deliver()
            self.assertEqual(self.items.count(), 5)
            self.assertEqual(sent, 0, "Receipt keys must commit before sending")
            self.deliver()
            self.assertEqual(peak, 4)
            self.assertEqual(sent, 4)
            self.assertEqual(self.items.filter(status="completed").count(), 4)
            self.assertEqual(self.items.filter(status="queued").count(), 1)
            self.deliver()
            self.assertEqual(sent, 5)
            self.deliver()

        self.assertEqual(self.items.filter(status="completed").count(), 5)
        self.assertEqual(self.index.status, "ready")

    @patch("core.case_search.cloudflare.exchange")
    def test_each_upload_stops_before_sending_text_if_instance_becomes_public(
        self, exchange
    ):
        self.create_sources(2)
        namespace_path = f"/ai-search/namespaces/{self.index.namespace}"
        instance_path = namespace_path + f"/instances/{self.index.instance}"

        def privacy_check(request, *_):
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            path = urlsplit(request.full_url).path
            if path.endswith(namespace_path):
                return {"name": self.index.namespace}, 50
            self.assertTrue(path.endswith(instance_path))
            return {
                "id": self.index.instance,
                "public_endpoint_params": {"enabled": True},
            }, 50

        exchange.side_effect = privacy_check
        for _ in range(3):
            self.deliver()
            if self.index.error_code:
                break

        self.assertEqual(exchange.call_count, 4)
        self.assertEqual(self.index.error_code, "search_instance_private")
        self.assertFalse(self.items.filter(status="completed").exists())

    @patch("core.case_search.cloudflare.request")
    def test_status_listing_reconciles_receipts_with_two_fallback_checks_per_delivery(
        self, request
    ):
        first, second, *missing = self.stage_passages(5)
        checked_at = timezone.now() - timedelta(minutes=2)
        for item in (first, second, *missing):
            item.status = "poll"
            item.remote_id = item.filename[:-4]
            item.checked_at = checked_at
            item.save(update_fields=["status", "remote_id", "checked_at"])
        by_path = {cloudflare.item_path(item.remote_id): item for item in missing}

        def inspect(current, method, path, **kwargs):
            self.assertEqual(current.pk, self.index.pk)
            self.assertEqual(method, "GET")
            endpoint = urlsplit(path)
            if endpoint.path == "/items":
                filters = parse_qs(endpoint.query)
                self.assertEqual(filters.get("source"), ["builtin"])
                self.assertEqual(filters.get("per_page"), ["50"])
                return [self.response(second), self.response(first)]
            return self.response(by_path[endpoint.path])

        request.side_effect = inspect
        self.deliver()
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.items.filter(status="completed").count(), 4)
        self.assertEqual(self.items.filter(status="poll").count(), 1)
        self.deliver()
        self.deliver()
        self.assertEqual(self.index.status, "ready")
        self.assertEqual(self.items.filter(status="completed").count(), 5)
        self.assertEqual(request.call_count, 4)

    @patch("core.case_search.cloudflare.request")
    def test_absent_recent_poll_receipt_waits_for_its_own_due_time(self, request):
        due, recent = self.stage_passages(2)
        now = timezone.now()
        for item, checked in ((due, now - timedelta(minutes=2)), (recent, now)):
            item.status = "poll"
            item.remote_id = item.filename[:-4]
            item.checked_at = checked
            item.save(update_fields=["status", "remote_id", "checked_at"])
        request.return_value = [self.response(due)]

        self.deliver()
        recent.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(due.status, "completed")
        self.assertEqual(recent.status, "poll")
        self.assertEqual(recent.checked_at, now)

    @patch("core.case_search.cloudflare.request")
    def test_rate_limited_upload_keeps_the_other_receipt_and_delays_delivery(
        self, request
    ):
        accepted, deferred = self.stage_passages(2)
        deadline = timezone.now() + timedelta(minutes=2)

        def upload(current, method, path, *, file, **kwargs):
            self.assertEqual(current.pk, self.index.pk)
            self.assertEqual((method, path), ("POST", "/items"))
            key = file[0]
            if key == deferred.filename:
                raise cloudflare.HostedSearchError(
                    "search_rate_limit", retry_at=deadline
                )
            self.assertEqual(key, accepted.filename)
            return {"key": key, "id": key[:-4], "status": "completed"}

        request.side_effect = upload
        self.deliver()
        accepted.refresh_from_db()
        deferred.refresh_from_db()
        self.assertEqual(accepted.status, "completed")
        self.assertIn(deferred.status, ("queued", "recover"))
        self.assertEqual(self.index.failures, 0)
        delivery = DBTaskResult.objects.get(
            task_path="core.case_search.lifecycle.sync_index",
            args_kwargs__args=[str(self.index.pk), str(self.index.token)],
        )
        self.assertGreaterEqual(delivery.run_after, deadline)

    @patch("core.case_search.cloudflare.request")
    def test_repeated_provider_throttling_is_bounded(self, request):
        self.stage_passages(1)
        deadline = timezone.now() + timedelta(minutes=10)
        request.side_effect = cloudflare.HostedSearchError(
            "search_rate_limit", retry_at=deadline, provider_throttled=True
        )

        for attempt in range(1, 4):
            self.deliver()
            self.assertEqual(self.index.failures, attempt)
            delivery = DBTaskResult.objects.get(
                task_path="core.case_search.lifecycle.sync_index",
                args_kwargs__args=[str(self.index.pk), str(self.index.token)],
            )
            self.assertGreaterEqual(delivery.run_after, deadline)
        self.deliver()
        self.assertEqual(self.index.status, "failed")
        self.assertEqual(self.index.error_code, "search_rate_limit")

    @patch("core.case_search.cloudflare.request")
    def test_a_lost_upload_receipt_is_recovered_by_key_while_its_peer_completes(
        self, request
    ):
        acknowledged, lost = self.stage_passages(2)
        remote = {}

        def handle(current, method, path, **kwargs):
            self.assertEqual(current.pk, self.index.pk)
            if method == "GET":
                self.assertEqual(
                    parse_qs(urlsplit(path).query),
                    {"source": ["builtin"], "key": [lost.filename]},
                )
                return [remote[lost.filename]]
            self.assertEqual((method, path), ("POST", "/items"))
            key = kwargs["file"][0]
            response = {"key": key, "id": key[:-4], "status": "completed"}
            remote[key] = response
            if key == lost.filename:
                raise cloudflare.HostedSearchError("search_unavailable")
            return response

        request.side_effect = handle
        self.deliver()
        acknowledged.refresh_from_db()
        lost.refresh_from_db()
        self.assertEqual(acknowledged.status, "completed")
        self.assertEqual(lost.status, "recover")

        self.deliver()
        lost.refresh_from_db()
        self.assertEqual(lost.status, "completed")
        self.assertEqual(lost.remote_id, remote[lost.filename]["id"])
        self.deliver()
        self.assertEqual(self.index.status, "ready")
        self.assertEqual(
            [call.args[1] for call in request.call_args_list], ["POST", "POST", "GET"]
        )
