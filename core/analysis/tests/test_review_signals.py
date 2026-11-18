from django.test import SimpleTestCase

from core.analysis.review_signals import classify_review_signal_v1


class ReviewSignalTests(SimpleTestCase):
    def test_native_reply_requires_a_matching_cited_message(self):
        finding = {
            "label": "Candidate exchange sequence",
            "source_ids": ["left", "right"],
            "values": {
                "before": {"source_id": "left", "native_message_id": "a"},
                "after": {"source_id": "right", "reply_to_id": "b"},
            },
        }
        self.assertEqual(classify_review_signal_v1("conversation_link", finding), "")
        finding["values"]["after"]["reply_to_id"] = "a"
        self.assertEqual(
            classify_review_signal_v1("conversation_link", finding), "native_reply"
        )
        finding["source_ids"] = ["right"]
        self.assertEqual(classify_review_signal_v1("conversation_link", finding), "")

    def test_clock_comparison_uses_known_offsets(self):
        finding = {
            "label": "Observation quality",
            "source_ids": ["location"],
            "values": {
                "Receipt time": "2026-06-10T14:00:00+00:00",
                "Event time": "2026-06-10T12:00:00-04:00",
            },
        }
        self.assertEqual(
            classify_review_signal_v1("location_quality", finding), "clock_order"
        )
        for event in (
            "2026-06-10T12:00:00",
            "Not recorded",
            "2026-06-10T12:00:00+00:00",
        ):
            finding["values"]["Event time"] = event
            self.assertEqual(classify_review_signal_v1("location_quality", finding), "")

    def test_uncertain_speech_requires_an_inspectable_source_interval(self):
        finding = {
            "source_ids": ["recording"],
            "values": {"Model uncertainty": "Review uncertain speech"},
            "anchors": [
                {"source_id": "recording", "start_seconds": 1.0, "end_seconds": 4.5}
            ],
        }
        self.assertEqual(
            classify_review_signal_v1("transcription", finding), "uncertain_speech"
        )
        finding["anchors"][0]["end_seconds"] = 1.0
        self.assertEqual(classify_review_signal_v1("transcription", finding), "")
