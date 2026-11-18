"""Native AAC M4A playback from the case-authorized original reader."""

from time import monotonic

from django.core.files.base import ContentFile
from playwright.sync_api import expect

from audio.models import AudioEvidence
from audio.testing import m4a_bytes
from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase


class M4ARecordingBrowserTests(BrowserTestCase):
    artifact_subdirectory = "m4a-recording-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("m4a-recording-owner")
        self.viewer = self.make_user("m4a-recording-viewer")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic recordings")
        CaseMembership.objects.create(case=self.case, user=self.viewer)
        self.recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic M4A recording",
            file=ContentFile(
                m4a_bytes(seconds=2, channels=2, rate=44100), name="recording.m4a"
            ),
        )

    def test_viewer_plays_and_seeks_m4a_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, user=self.viewer)
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="audio",
                        evidence_id=self.recording.pk,
                    )
                )
                expect(
                    page.get_by_text("Original M4A recording", exact=False)
                ).to_be_visible()
                audio = page.get_by_label("Play Synthetic M4A recording", exact=True)
                expect(audio).to_be_visible()
                expect(page.locator("audio source")).to_have_attribute(
                    "type", "audio/mp4"
                )
                audio.evaluate("node => { node.preload = 'metadata'; node.load(); }")
                # Browser timers are disabled with page JavaScript. Poll from
                # Python so the no-JavaScript journey still waits for media I/O.
                deadline = monotonic() + 15
                while monotonic() < deadline:
                    state = audio.evaluate(
                        """node => ({
                            ready: node.readyState,
                            error: node.error && node.error.message,
                            network: node.networkState
                        })"""
                    )
                    if state["ready"] >= 1 or state["error"] is not None:
                        break
                    page.wait_for_timeout(100)
                self.assertGreaterEqual(state["ready"], 1, f"{engine}: {state}")
                self.assertIsNone(
                    audio.evaluate("node => node.error && node.error.message"),
                    f"{engine} could not decode the synthetic AAC M4A recording",
                )
                self.assertAlmostEqual(
                    audio.evaluate("node => node.duration"), 2, delta=0.1
                )
                audio.evaluate("node => { node.currentTime = 0.5; }")
                expect(audio).to_have_js_property("currentTime", 0.5)
                audio.evaluate("node => node.play()")
                expect(audio).to_have_js_property("paused", False)
                audio.evaluate("node => node.pause()")
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
                    self.capture(page, engine, f"m4a-{width}x{height}")
                audio.focus()
                expect(audio).to_be_focused()
                self.capture(page, engine, "m4a-focus-320")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 321
                )
                self.capture(page, engine, "m4a-200-percent-text-320")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.set_viewport_size({"width": 390, "height": 844})
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "m4a-dark-390")
                page.emulate_media(forced_colors="active", reduced_motion="reduce")
                self.capture(page, engine, "m4a-forced-colors-390")
