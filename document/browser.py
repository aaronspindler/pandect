"""Read synthetic originals and seek recordings in both supported browsers."""

from urllib.parse import urljoin

from django.core.files.base import ContentFile
from playwright.sync_api import expect

from audio.models import AudioEvidence
from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.testing import wav_bytes
from document.models import DocumentEvidence
from document.tests.test_analysis import pdf_bytes


class OriginalPreviewBrowserTests(BrowserTestCase):
    artifact_subdirectory = "original-preview-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("original-preview-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic originals")
        self.pdf = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Two-page original",
            file=ContentFile(
                pdf_bytes("Synthetic page one", "Synthetic page two"), name="pages.pdf"
            ),
        )
        self.text = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Escaped text original",
            file=ContentFile(
                b"  <script>source text</script>\r\nSecond line  ", name="text.txt"
            ),
        )
        self.recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic recording",
            file=ContentFile(wav_bytes(seconds=3), name="recording.wav"),
        )
        self.bad = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Encrypted original",
            file=ContentFile(
                pdf_bytes("Encrypted", encrypted=True), name="encrypted.pdf"
            ),
        )

    def detail_url(self, item, kind="document"):
        return self.url(
            "core:evidence:detail", case_id=self.case.pk, kind=kind, evidence_id=item.pk
        )

    def test_pdf_text_and_recording_readers_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.detail_url(self.pdf))
                preview = page.get_by_role("img", name="Page 1 of Two-page original")
                expect(preview).to_be_visible()
                page.wait_for_function(
                    "document.querySelector('#document-preview img').naturalWidth > 0"
                )
                next_page = page.get_by_role("link", name="Next page", exact=True)
                next_page.focus()
                expect(next_page).to_be_focused()
                next_page.press("Enter")
                expect(page.get_by_text("Page 2 of 2", exact=True)).to_be_visible()
                expect(
                    page.get_by_role("img", name="Page 2 of Two-page original")
                ).to_have_js_property("naturalWidth", 400)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"pdf-{width}x{height}")
                page.goto(self.detail_url(self.text))
                expect(
                    page.get_by_role("region", name="Decoded document text")
                ).to_have_text("  <script>source text</script>\nSecond line  ")
                self.assertEqual(page.locator(".evidence-original script").count(), 0)
                self.capture(page, engine, "utf8-320")
                page.goto(self.detail_url(self.bad))
                expect(
                    page.get_by_text("This PDF is encrypted.", exact=False)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="Download original", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "encrypted-fallback-320")
                page.goto(self.detail_url(self.recording, "audio"))
                audio = page.get_by_label("Play Synthetic recording", exact=True)
                expect(audio).to_be_visible()
                audio.evaluate("node => { node.preload = 'metadata'; node.load(); }")
                expect(audio).to_have_js_property("duration", 3)
                audio.evaluate("node => { node.currentTime = 1.5; }")
                expect(audio).to_have_js_property("currentTime", 1.5)
                source = page.locator("audio source").get_attribute("src")
                response = page.request.get(
                    urljoin(page.url, source), headers={"Range": "bytes=20-39"}
                )
                self.assertEqual(response.status, 206)
                self.assertEqual(len(response.body()), 20)
                self.capture(page, engine, "audio-320")

    def test_native_recording_playback_and_pdf_appearance(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.detail_url(self.recording, "audio"))
                audio = page.get_by_label("Play Synthetic recording", exact=True)
                audio.evaluate("node => node.play()")
                expect(audio).to_have_js_property("paused", False)
                audio.evaluate("node => { node.pause(); node.currentTime = 1.5; }")
                expect(audio).to_have_js_property("currentTime", 1.5)
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "audio-playing-390")
                page.goto(self.detail_url(self.pdf))
                expect(
                    page.get_by_role("img", name="Page 1 of Two-page original")
                ).to_have_js_property("naturalWidth", 400)
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "pdf-dark-390")
                page.emulate_media(forced_colors="active")
                self.capture(page, engine, "pdf-forced-colors-390")

    def test_viewer_loses_subsequent_preview_access(self):
        viewer = self.make_user("original-preview-viewer")
        CaseMembership.objects.create(case=self.case, user=viewer)
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, user=viewer)
                page.goto(self.detail_url(self.pdf))
                image = page.get_by_role("img", name="Page 1 of Two-page original")
                expect(image).to_be_visible()
                source = urljoin(page.url, image.get_attribute("src"))
                self.assertEqual(page.request.get(source).status, 200)
                self.db(
                    CaseMembership.objects.filter(case=self.case, user=viewer).delete
                )
                self.assertEqual(page.request.get(source).status, 404)
                self.db(CaseMembership.objects.create, case=self.case, user=viewer)
