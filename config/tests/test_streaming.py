import asyncio
import hashlib
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.handlers.asgi import ASGIHandler
from django.http import StreamingHttpResponse
from django.test import AsyncRequestFactory, SimpleTestCase, TransactionTestCase

from cases.models import CaseMembership
from config.streaming import AsyncStreamingMiddleware
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence


class ExportStreamingTests(SimpleTestCase):
    async def test_json_export_encodes_incrementally_in_bounded_blocks(self):
        entries = ["Synthetic entry"] * 13000 + ["x" * 150000]
        generated = 0

        def encode():
            nonlocal generated
            for fragment in json.JSONEncoder().iterencode(entries):
                generated += len(fragment)
                yield fragment

        response = AsyncStreamingMiddleware(
            lambda request: StreamingHttpResponse(
                encode(), content_type="application/json"
            )
        )(AsyncRequestFactory().get("/"))
        self.assertTrue(response.is_async)
        encoded = bytearray()
        async for part in response:
            self.assertLessEqual(len(part), ASGIHandler.chunk_size)
            if not encoded:
                self.assertEqual(len(part), ASGIHandler.chunk_size)
                self.assertLess(generated, 2 * ASGIHandler.chunk_size)
            encoded.extend(part)
        self.assertEqual(json.loads(encoded), entries)


class PrivateDownloadStreamingTests(EvidenceViewTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.login("viewer")
        self.cookie = self.client.cookies.output(header="", sep=";").strip().encode()
        content = bytes(range(256)) * 8193
        self.item = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic original",
            file=SimpleUploadedFile("original.bin", content),
        )
        self.digest = hashlib.sha256(content).digest()
        self.length = len(content)

    async def download(self, *, method="GET", disconnect=False, case=None):
        scope = {
            "type": "http",
            "method": method,
            "path": self.url("download", self.item, "document", case=case),
            "headers": [(b"host", b"testserver"), (b"cookie", self.cookie)],
            "query_string": b"",
        }
        inputs = asyncio.Queue()
        await inputs.put({"type": "http.request"})
        start = {}
        sent = 0
        digest = hashlib.sha256()
        handles = []
        read_bytes = 0
        open_file = self.item.file.storage._open

        def track_file(*args):
            file = open_file(*args)
            handles.append(file)
            original_read = file.read

            def read(size):
                nonlocal read_bytes
                self.assertLessEqual(0, size)
                self.assertLessEqual(size, ASGIHandler.chunk_size)
                data = original_read(size)
                read_bytes += len(data)
                return data

            file.file.read = read
            return file

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.start":
                start.update(message)
                return
            body = message.get("body", b"")
            digest.update(body)
            sent += len(body)
            if handles:
                # The server has consumed only bytes delivered to its sender so far.
                self.assertEqual(read_bytes, sent)
            if disconnect and body:
                await inputs.put({"type": "http.disconnect"})
                await asyncio.Event().wait()

        with patch.object(self.item.file.storage, "_open", side_effect=track_file):
            async with asyncio.timeout(10):
                await ASGIHandler()(scope, inputs.get, send)
        self.assertTrue(all(file.closed for file in handles))
        return start, sent, digest.digest(), handles

    async def test_original_streams_with_backpressure_and_closes_on_disconnect_or_head(
        self,
    ):
        response, length, digest, handles = await self.download()
        self.assertEqual(response["status"], 200)
        headers = {key.lower(): value for key, value in response["headers"]}
        self.assertEqual(headers[b"content-length"], str(self.length).encode())
        self.assertEqual(
            headers[b"content-disposition"], b'attachment; filename="original.bin"'
        )
        self.assertEqual(headers[b"content-type"], b"application/octet-stream")
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual((length, digest, len(handles)), (self.length, self.digest, 1))

        response, length, _, handles = await self.download(disconnect=True)
        self.assertEqual(
            (response["status"], length, len(handles)), (200, ASGIHandler.chunk_size, 1)
        )
        response, length, _, handles = await self.download(method="HEAD")
        self.assertEqual((response["status"], length, len(handles)), (200, 0, 1))
        self.assertIn(
            (b"Content-Length", str(self.length).encode()), response["headers"]
        )

    async def test_cross_case_and_revoked_downloads_never_open_the_file(self):
        response, _, _, handles = await self.download(case=self.other_case)
        self.assertEqual((response["status"], handles), (404, []))
        await CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).adelete()
        response, _, _, handles = await self.download()
        self.assertEqual((response["status"], handles), (404, []))
