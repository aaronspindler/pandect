import hashlib
import io
import struct
import zlib
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.parsers import MAX_BYTES
from core.testing import EvidenceViewTestMixin
from image.models import ImageEvidence
from image.testing import jpeg_bytes


def image_bytes(format="PNG", size=(80, 50), **options):
    buffer = io.BytesIO()
    with Image.new("RGB", size, "#336699") as image:
        image.save(buffer, format=format, **options)
    return buffer.getvalue()


class ImagePreviewTests(EvidenceViewTestMixin, TestCase):
    def image(self, content=None, **values):
        return ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Harbour photograph",
            file=SimpleUploadedFile(
                "source.bin",
                image_bytes() if content is None else content,
                content_type="text/html",
            ),
            **values,
        )

    def test_current_case_members_can_preview_even_when_archived(self):
        item = self.image()
        self.login("owner")
        self.client.post(self.url("archive", item, kind="image"))
        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                self.login(role)
                response = self.client.get(self.url("preview", item, kind="image"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/jpeg")
                self.assertTrue(response["Content-Disposition"].startswith("inline;"))
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                with Image.open(io.BytesIO(response.content)) as preview:
                    self.assertEqual(preview.size, (80, 50))
                detail = self.client.get(self.url("detail", item, kind="image"))
                self.assertContains(detail, self.url("preview", item, kind="image"))
                self.assertContains(detail, 'alt="Image preview of Harbour photograph"')
                self.assertNotContains(detail, item.file.name)

    def test_preview_rechecks_authentication_verification_and_revoked_access(self):
        item = self.image()
        url = self.url("preview", item, kind="image")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login("viewer")
        self.assertEqual(self.client.get(url).status_code, 200)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("owner")
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_preview_rejects_outsiders_cross_case_ids_other_kinds_and_writes(self):
        item = self.image()
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.url("preview", item, kind="image")).status_code, 404
        )
        self.login("owner")
        with patch("image.previews.run_parser") as parser:
            self.assertEqual(
                self.client.get(
                    self.url("preview", item, kind="image", case=self.other_case)
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.get(
                    self.url("preview", self.text(), kind="text")
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.post(self.url("preview", item, kind="image")).status_code,
                405,
            )
            parser.assert_not_called()

    def test_jpeg_is_oriented_resized_and_stripped_without_changing_original(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "Synthetic author"
        original = image_bytes("JPEG", size=(2400, 1200), exif=exif)
        item = self.image(original)
        self.login("owner")
        response = self.client.get(self.url("preview", item, kind="image"))
        self.assertEqual(response.status_code, 200)
        with Image.open(io.BytesIO(response.content)) as preview:
            self.assertEqual(preview.size, (1024, 2048))
            self.assertFalse(preview.getexif())
        item.refresh_from_db()
        self.assertEqual(item.sha256, hashlib.sha256(original).hexdigest())
        download = self.client.get(self.url("download", item, kind="image"))
        self.assertEqual(b"".join(download.streaming_content), original)
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))

    def test_transparency_is_flattened_onto_white(self):
        buffer = io.BytesIO()
        with Image.new("RGBA", (8, 8), (255, 0, 0, 0)) as image:
            image.save(buffer, format="PNG")
        item = self.image(buffer.getvalue())
        self.login("viewer")
        response = self.client.get(self.url("preview", item, kind="image"))
        with Image.open(io.BytesIO(response.content)) as preview:
            self.assertEqual(preview.mode, "RGB")
            self.assertEqual(preview.getpixel((0, 0)), (255, 255, 255))

    def test_iphone_jpeg_exports_preview_without_changing_originals(self):
        self.login("owner")
        for content, size, preview_size in (
            (jpeg_bytes(size=(8064, 6048)), (8064, 6048), (2048, 1536)),
            (jpeg_bytes(multiple_pictures=True), (1000, 240), (1000, 240)),
        ):
            with self.subTest(size=size):
                response = self.client.post(
                    self.url("create"),
                    self.payload(
                        "image",
                        file=SimpleUploadedFile(
                            "iphone-export.jpeg", content, "image/jpeg"
                        ),
                    ),
                    HTTP_ACCEPT="application/json",
                )
                self.assertEqual(response.status_code, 201)
                item = ImageEvidence.objects.get(pk=response.json()["id"])
                preview = self.client.get(self.url("preview", item, kind="image"))
                self.assertEqual(preview.status_code, 200)
                with Image.open(io.BytesIO(preview.content)) as decoded:
                    self.assertEqual(decoded.size, preview_size)
                    self.assertEqual(decoded.format, "JPEG")
                download = self.client.get(self.url("download", item, kind="image"))
                self.assertEqual(b"".join(download.streaming_content), content)
                self.assertEqual(item.sha256, hashlib.sha256(content).hexdigest())

    def test_unsupported_active_malformed_and_animated_content_is_never_embedded(self):
        animated = io.BytesIO()
        with Image.new("RGB", (8, 8), "red") as first:
            with Image.new("RGB", (8, 8), "blue") as second:
                first.save(
                    animated, format="PNG", save_all=True, append_images=[second]
                )
        self.login("owner")
        for content in (
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
            b"<html><script>alert(1)</script></html>",
            b"\x89PNG\r\n\x1a\ninvalid",
            animated.getvalue(),
        ):
            with self.subTest(content=content[:20]):
                item = self.image(content)
                response = self.client.get(self.url("preview", item, kind="image"))
                self.assertEqual(response.status_code, 422)
                self.assertTrue(response["Content-Type"].startswith("text/plain"))
                self.assertIn("no-store", response["Cache-Control"])
                self.assertNotIn(content, response.content)
                self.assertContains(
                    self.client.get(self.url("detail", item, kind="image")),
                    self.url("download", item, kind="image"),
                )

    def test_oversized_source_is_rejected_before_reading_or_decoding(self):
        item = self.image()
        ImageEvidence.objects.filter(pk=item.pk).update(byte_size=MAX_BYTES + 1)
        self.login("owner")
        with patch("image.previews.run_parser") as parser:
            response = self.client.get(self.url("preview", item, kind="image"))
            self.assertEqual(response.status_code, 422)
            parser.assert_not_called()

    def test_pixel_limit_is_checked_before_decoding_pixels(self):
        content = image_bytes()
        header = struct.pack(">II", 10001, 5000) + content[24:29]
        checksum = struct.pack(">I", zlib.crc32(b"IHDR" + header))
        item = self.image(content[:16] + header + checksum + content[33:])
        self.login("owner")
        self.assertEqual(
            self.client.get(self.url("preview", item, kind="image")).status_code, 422
        )

    def test_changed_or_missing_source_and_parser_failure_keep_safe_fallback(self):
        item = self.image()
        self.login("owner")
        url = self.url("preview", item, kind="image")
        with patch(
            "image.previews.run_parser",
            side_effect=AnalysisError("parser_timeout"),
        ):
            self.assertEqual(self.client.get(url).status_code, 422)
        with item.file.storage.open(item.file.name, "wb") as stream:
            original = image_bytes()
            stream.write(original[:-1] + bytes([original[-1] ^ 1]))
        with patch("image.previews.run_parser") as parser:
            self.assertEqual(self.client.get(url).status_code, 422)
            parser.assert_not_called()
        item.file.storage.delete(item.file.name)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(str(self.media_root), response.content.decode())
        self.assertTrue(ImageEvidence.objects.filter(pk=item.pk).exists())
