"""Exercise the deployed ASGI callable, including the order of body receipt."""

import asyncio
import json
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from asgiref.sync import async_to_sync, sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse

from cases.models import CaseMembership
from config.asgi import application
from config.http_body import PandectASGIHandler, RequestBody
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence


class UnifiedASGITests(EvidenceViewTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.upload = reverse("core:evidence:create", args=[self.case.pk])

    def request(
        self,
        path,
        *,
        role=None,
        chunks=(),
        headers=(),
        method="POST",
        content_type=MULTIPART_CONTENT,
        disconnect=False,
    ):
        cookies = {settings.CSRF_COOKIE_NAME: "a" * 32}
        if role:
            self.login(role)
            cookies[settings.SESSION_COOKIE_NAME] = self.client.session.session_key
        scope = {
            "type": "http",
            "path": path,
            "method": method,
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", content_type.encode()),
                (b"accept", b"application/json"),
                (
                    b"cookie",
                    "; ".join(
                        f"{key}={value}" for key, value in cookies.items()
                    ).encode(),
                ),
                *headers,
            ],
        }

        async def exchange():
            received, sent = [], []
            remaining = iter(chunks)
            ended = False

            async def receive():
                nonlocal ended
                if ended:
                    await asyncio.Future()
                chunk = next(remaining, None)
                if chunk is not None:
                    received.append(len(chunk))
                    return {"type": "http.request", "body": chunk, "more_body": True}
                if disconnect:
                    return {"type": "http.disconnect"}
                if not received:
                    self.fail("The rejected request consumed its body.")
                ended = True
                return {"type": "http.request", "body": b""}

            async def send(message):
                sent.append(message)

            async with asyncio.timeout(5):
                await application(scope, receive, send)
            return sent, received

        return async_to_sync(exchange)()

    def test_account_case_and_unknown_route_rejected_without_receiving_body(self):
        for role, status in ((None, 401), ("viewer", 403), ("outsider", 404)):
            with self.subTest(role=role):
                sent, received = self.request(self.upload, role=role)
                self.assertEqual(sent[0]["status"], status)
                self.assertFalse(received)
                self.assertIn(
                    b"no-store",
                    dict((k.lower(), v) for k, v in sent[0]["headers"])[
                        b"cache-control"
                    ],
                )
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.request(self.upload, role="owner")[0][0]["status"], 403)
        CaseMembership.objects.filter(user=self.users["editor"]).delete()
        self.assertEqual(self.request(self.upload, role="editor")[0][0]["status"], 404)
        self.assertEqual(self.request("/missing/")[0][0]["status"], 404)

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=4096, FILE_UPLOAD_MAX_MEMORY_SIZE=64)
    def test_only_permitted_admin_original_additions_accept_large_bodies(self):
        for role in ("owner", "editor", "viewer"):
            self.users[role].is_staff = True
            self.users[role].save(update_fields=["is_staff"])
        add_document = Permission.objects.get(
            content_type__app_label="document", codename="add_documentevidence"
        )
        self.users["editor"].user_permissions.add(add_document)
        self.users["viewer"].user_permissions.add(add_document)
        add = reverse("admin:document_documentevidence_add")
        for role in ("owner", "viewer"):
            # The owner lacks model permission and the viewer has no editable case.
            sent, _ = self.request(add, role=role)
            self.assertEqual(sent[0]["status"], 403)

        for path in (
            reverse("admin:index"),
            reverse("admin:document_documentevidence_changelist"),
            reverse("admin:document_documentevidence_change", args=[uuid4()]),
        ):
            sent, _ = self.request(
                path, role="editor", headers=[(b"content-length", b"4097")]
            )
            self.assertEqual(sent[0]["status"], 413)
        csrf = {"csrfmiddlewaretoken": "a" * 32}
        sent, _ = self.request(
            reverse("admin:index"),
            role="editor",
            chunks=[encode_multipart(BOUNDARY, csrf)],
        )
        self.assertEqual(sent[0]["status"], 200)

        for case, status in ((self.other_case, 200), (self.case, 302)):
            encoded = encode_multipart(
                BOUNDARY,
                {
                    **csrf,
                    "case": case.pk,
                    "title": "Admin original",
                    "_save": "Save",
                    "file": SimpleUploadedFile("source.txt", b"x" * 8192),
                },
            )
            sent, _ = self.request(add, role="editor", chunks=[encoded])
            self.assertEqual(sent[0]["status"], status)
            self.assertFalse(
                DocumentEvidence.objects.filter(case=self.other_case).exists()
            )
        self.assertEqual(DocumentEvidence.objects.get().case_id, self.case.pk)

    @override_settings(
        PANDECT_MAX_FILE_BYTES=1024,
        DATA_UPLOAD_MAX_MEMORY_SIZE=512,
        FILE_UPLOAD_MAX_MEMORY_SIZE=8,
    )
    def test_declared_chunked_and_interrupted_bodies_are_bounded(self):
        sent, _ = self.request(
            self.upload, role="owner", headers=[(b"content-length", b"1537")]
        )
        self.assertEqual(sent[0]["status"], 413)
        writes = []
        original = RequestBody.write_to_disk

        def track_write(body, chunk):
            writes.append(len(chunk))
            return original(body, chunk)

        with patch.object(RequestBody, "write_to_disk", track_write):
            sent, received = self.request(
                self.upload, role="editor", chunks=[b"x" * 512] * 5
            )
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(received, [512] * 4)
        self.assertEqual(writes, [512] * 3)
        for method, content_type in (
            ("PUT", "multipart/form-data"),
            ("POST", "text/plain"),
        ):
            with self.subTest(method=method):
                sent, _ = self.request(
                    self.upload,
                    method=method,
                    content_type=content_type,
                    headers=[(b"content-length", b"513")],
                )
                self.assertEqual(sent[0]["status"], 413)
        sent, _ = self.request(
            self.upload, role="owner", chunks=[b"x" * 32], disconnect=True
        )
        self.assertEqual(sent[0]["status"], 400)
        self.assertFalse(DocumentEvidence.objects.exists())

    @override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=8192)
    def test_chunked_authorized_file_keeps_original_bytes_and_csrf(self):
        original = b"preserved original\r\n" * 7000
        for csrf, status in ((False, 403), (True, 201)):
            fields = {
                "kind": "document",
                "title": "Synthetic",
                "submission_id": str(uuid4()),
                "file": SimpleUploadedFile("source.txt", original),
            }
            if csrf:
                fields["csrfmiddlewaretoken"] = "a" * 32
            encoded = encode_multipart(BOUNDARY, fields)
            sent, _ = self.request(
                self.upload,
                role="editor",
                chunks=[encoded[i : i + 8192] for i in range(0, len(encoded), 8192)],
            )
            self.assertEqual(sent[0]["status"], status)
        response = json.loads(sent[1]["body"])
        item = DocumentEvidence.objects.get(pk=response["id"])
        self.assertEqual(item.sha256, sha256(original).hexdigest())

    async def test_static_response_stops_on_disconnect_before_or_during_send(self):
        asset = self.media_root / "large.css"
        asset.write_bytes(b"x" * 256 * 1024)
        scope = {
            "type": "http",
            "path": "/static/large.css",
            "method": "GET",
            "headers": [(b"host", b"testserver")],
        }
        with self.settings(STATIC_ROOT=self.media_root):
            handler = PandectASGIHandler()

        async def interrupted(at_headers):
            incoming = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": b""})
            delivered = []

            async def send(message):
                if message["type"] == "http.response.start":
                    self.assertEqual(message["status"], 200)
                else:
                    delivered.append(len(message.get("body", b"")))
                if delivered or at_headers:
                    await incoming.put({"type": "http.disconnect"})
                    # Gunicorn HTTP/1 may have a write waiting for capacity.
                    # Django's disconnect watcher must cancel this await.
                    await asyncio.Future()

            async with asyncio.timeout(2):
                await handler(scope, incoming.get, send)
            self.assertEqual(delivered, [] if at_headers else [handler.chunk_size])

        for at_headers in (True, False):
            with self.subTest(at_headers=at_headers):
                await interrupted(at_headers)

    async def test_http_and_event_readiness_share_one_application(self):
        @sync_to_async
        def editor_cookie():
            self.login("editor")
            session_key = self.client.session.session_key
            return f"{settings.SESSION_COOKIE_NAME}={session_key}".encode()

        # Hold the upload in receive() while the same worker serves the
        # listener's readiness and an ordinary Django health request.
        upload_waiting = asyncio.Event()

        async def delayed(scope, receive, send):
            async def wait_for_body():
                upload_waiting.set()
                return await receive()

            await application(scope, wait_for_body, send)

        stalled = ApplicationCommunicator(
            delayed,
            {
                "type": "http",
                "method": "POST",
                "path": self.upload,
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", MULTIPART_CONTENT.encode()),
                    (b"cookie", await editor_cookie()),
                ],
            },
        )
        await stalled.send_input(
            {"type": "http.request", "body": b"a", "more_body": True}
        )
        await asyncio.wait_for(upload_waiting.wait(), 5)
        life = ApplicationCommunicator(application, {"type": "lifespan"})
        await life.send_input({"type": "lifespan.startup"})
        self.assertEqual(
            (await life.receive_output())["type"], "lifespan.startup.complete"
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    events = ApplicationCommunicator(
                        application,
                        {
                            "type": "http",
                            "root_path": "/pandect",
                            "path": "/pandect/events/health/",
                            "method": "GET",
                            "headers": [(b"host", b"testserver")],
                        },
                    )
                    ready = (await events.receive_output())["status"]
                    await events.receive_output()
                    await events.wait()
                    if ready == 200:
                        break
                    await asyncio.sleep(0.01)
            web = ApplicationCommunicator(
                application,
                {
                    "type": "http",
                    "path": "/health/",
                    "method": "GET",
                    "headers": [(b"host", b"testserver")],
                },
            )
            await web.send_input({"type": "http.request", "body": b""})
            self.assertEqual((await web.receive_output())["status"], 200)
            await web.receive_output()
            await web.wait()
        finally:
            await stalled.send_input({"type": "http.disconnect"})
            await stalled.wait()
            await life.send_input({"type": "lifespan.shutdown"})
            self.assertEqual(
                (await life.receive_output())["type"], "lifespan.shutdown.complete"
            )
            await life.wait()
