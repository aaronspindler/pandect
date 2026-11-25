import asyncio
import json

from allauth.account.models import EmailAddress
from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.contrib.sessions.models import Session
from django.db import transaction
from django.test import TransactionTestCase
from django.urls import reverse

from cases.events import CaseEvents
from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from people.models import Person


class CaseEventTests(EvidenceViewTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.sessions = {}
        for role in self.users:
            self.login(role)
            self.sessions[role] = self.client.session.session_key
            self.client.cookies.clear()
        self.service = CaseEvents()

    def request(self, role="viewer", *, case=None, path=None, headers=()):
        cookie = f"{settings.SESSION_COOKIE_NAME}={self.sessions.get(role, '')}"
        return ApplicationCommunicator(
            self.service,
            {
                "type": "http",
                "path": path or reverse("case_events", args=[(case or self.case).pk]),
                "method": "GET",
                "headers": [
                    (b"host", b"testserver"),
                    (b"cookie", cookie.encode()),
                    *headers,
                ],
            },
        )

    async def open_stream(self, role="viewer"):
        stream = self.request(role)
        response = await stream.receive_output()
        self.assertEqual(response["status"], 200)
        headers = dict(response["headers"])
        self.assertEqual(headers[b"content-type"], b"text/event-stream")
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertIn(b": connected", (await stream.receive_output())["body"])
        return stream

    async def test_committed_changes_only_and_listener_readiness(self):
        lifespan = ApplicationCommunicator(self.service, {"type": "lifespan"})
        await lifespan.send_input({"type": "lifespan.startup"})
        await lifespan.receive_output()
        try:
            async with asyncio.timeout(5):
                while not self.service.listening:
                    await asyncio.sleep(0.01)
            health = self.request(path="/events/health/")
            self.assertEqual((await health.receive_output())["status"], 200)
            body = json.loads((await health.receive_output())["body"])
            self.assertEqual(
                body,
                {
                    "status": "ok",
                    "version": settings.APP_VERSION,
                    "commit": settings.APP_COMMIT_SHA,
                },
            )
            stream = await self.open_stream()

            @sync_to_async
            def discarded_or_unrelated():
                with transaction.atomic():
                    Person.objects.create(case=self.case, first_name="Discarded")
                    transaction.set_rollback(True)
                Person.objects.create(case=self.other_case, first_name="Other case")

            await discarded_or_unrelated()
            self.assertTrue(await stream.receive_nothing(timeout=0.1))
            await Person.objects.acreate(case=self.case, first_name="Private name")
            self.assertEqual(
                (await stream.receive_output())["body"], b"event: change\ndata: {}\n\n"
            )
            await CaseMembership.objects.filter(
                case=self.case, user=self.users["viewer"]
            ).adelete()
            await Person.objects.acreate(case=self.case, first_name="After revocation")
            message = await stream.receive_output()
            self.assertEqual(message["body"], b"event: access-lost\ndata: {}\n\n")
            self.assertFalse(message.get("more_body"))
            await stream.wait()
            self.assertFalse(self.service.subscribers)
        finally:
            await lifespan.send_input({"type": "lifespan.shutdown"})
            await lifespan.receive_output()
            await lifespan.wait()

    async def test_authentication_scope_and_an_open_session_ending(self):
        self.service.listening = True
        for role in ("viewer", "editor", "owner"):
            stream = await self.open_stream(role)
            await stream.send_input({"type": "http.disconnect"})
            await stream.wait()
        for stream in (
            self.request("outsider"),
            self.request("anonymous"),
            self.request(case=self.other_case),
        ):
            self.assertEqual((await stream.receive_output())["status"], 404)
        cross_origin = self.request(headers=[(b"origin", b"https://other.example")])
        self.assertEqual((await cross_origin.receive_output())["status"], 403)
        await EmailAddress.objects.filter(user=self.users["owner"]).aupdate(
            verified=False
        )
        self.assertEqual((await self.request("owner").receive_output())["status"], 404)
        stream = await self.open_stream()
        await Session.objects.filter(session_key=self.sessions["viewer"]).adelete()
        for wake in self.service.subscribers[str(self.case.pk)]:
            wake.set()
        self.assertIn(b"event: access-lost", (await stream.receive_output())["body"])
        await stream.wait()

    async def test_stream_capacity_released_on_disconnect(self):
        self.service.listening = True
        self.service.slots = asyncio.Semaphore(1)
        stream = await self.open_stream()
        self.assertEqual((await self.request().receive_output())["status"], 503)
        await stream.send_input({"type": "http.disconnect"})
        await stream.wait()
        another = await self.open_stream()
        await another.send_input({"type": "http.disconnect"})
        await another.wait()

    async def test_role_change_closes_an_authorized_stream(self):
        self.service.listening = True
        stream = await self.open_stream("editor")
        await CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).aupdate(role="viewer")
        for wake in self.service.subscribers[str(self.case.pk)]:
            wake.set()
        self.assertEqual(
            (await stream.receive_output())["body"], b"event: access-lost\ndata: {}\n\n"
        )
        await stream.wait()
        replacement = await self.open_stream("editor")
        await replacement.send_input({"type": "http.disconnect"})
        await replacement.wait()
