"""Original recording access, format validation, and byte-range seeking."""

from allauth.account.models import EmailAddress
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from audio.models import AudioEvidence
from cases.models import CaseMembership
from core.analysis.testing import wav_bytes
from core.testing import EvidenceViewTestMixin
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class RecordingPreviewTests(EvidenceViewTestMixin, TestCase):
    def recording(self, content=None):
        return AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic recording",
            file=SimpleUploadedFile(
                "recording.wav", wav_bytes() if content is None else content
            ),
        )

    def play_url(self, item, case=None):
        return reverse(
            "core:evidence:audio_play",
            kwargs={"case_id": (case or self.case).pk, "evidence_id": item.pk},
        )

    def test_roles_play_without_analysis_and_original_bytes_are_preserved(self):
        item = self.recording()
        original = item.file.read()
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.play_url(item))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, original)
            self.assertEqual(response["Content-Type"], "audio/wav")
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(
            self.client.get(self.url("detail", item, kind="audio")), self.play_url(item)
        )
        download = self.client.get(self.url("download", item, kind="audio"))
        self.assertEqual(b"".join(download.streaming_content), original)

    def test_byte_ranges_head_suffix_invalid_and_if_range(self):
        item = self.recording()
        original = item.file.read()
        self.login("viewer")
        for header, expected in (
            ("bytes=2-5", original[2:6]),
            ("bytes=20-", original[20:]),
            ("bytes=-8", original[-8:]),
        ):
            response = self.client.get(self.play_url(item), HTTP_RANGE=header)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, expected)
            self.assertEqual(int(response["Content-Length"]), len(expected))
        for header in (
            "bytes=999999999-",
            "bytes=-0",
            "bytes=8-2",
            "bytes=1-2,4-5",
            "bytes=-",
            "invalid",
        ):
            self.assertEqual(
                self.client.get(self.play_url(item), HTTP_RANGE=header).status_code, 416
            )
        response = self.client.head(self.play_url(item), HTTP_RANGE="bytes=2-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Content-Length"], "4")
        response = self.client.get(
            self.play_url(item), HTTP_RANGE="bytes=2-5", HTTP_IF_RANGE='"old"'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, original)

    def test_current_access_and_unsupported_changed_truncated_recordings(self):
        item = self.recording()
        url = self.play_url(item)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("viewer")
        self.assertEqual(
            self.client.get(self.play_url(item, self.other_case)).status_code, 404
        )
        self.assertEqual(self.client.post(url).status_code, 405)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("owner")
        for content in (b"<html>active content</html>", wav_bytes()[:-10]):
            bad = self.recording(content)
            self.assertEqual(self.client.get(self.play_url(bad)).status_code, 422)
            self.assertContains(
                self.client.get(self.url("detail", bad, kind="audio")),
                "Inline playback supports",
            )
        AudioEvidence.objects.filter(pk=item.pk).update(sha256="0" * 64)
        self.assertEqual(self.client.get(url).status_code, 422)
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_call_recording_checks_exact_parent_and_audio_hash(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Call",
            sender=self.person,
            recipient=self.person,
        )
        other = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other call",
            sender=self.person,
            recipient=self.person,
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="Supplied transcript",
            file=SimpleUploadedFile("call.wav", wav_bytes()),
        )
        kwargs = {
            "case_id": self.case.pk,
            "kind": "phone-call",
            "evidence_id": call.pk,
            "attachment_id": attachment.pk,
        }
        url = reverse("phone:attachment_play", kwargs=kwargs)
        self.login("viewer")
        self.assertEqual(self.client.get(url).content, wav_bytes())
        self.assertContains(
            self.client.get(self.url("detail", call, kind="phone-call")), url
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "phone:attachment_play", kwargs=kwargs | {"evidence_id": other.pk}
                )
            ).status_code,
            404,
        )
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            audio_sha256="0" * 64
        )
        self.assertEqual(self.client.get(url).status_code, 422)
