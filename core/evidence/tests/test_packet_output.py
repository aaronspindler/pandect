from datetime import date

from django.test import SimpleTestCase
from django.utils.safestring import mark_safe

from core.evidence.packet_output import PacketExhibit, render_packet


class PacketOutputTests(SimpleTestCase):
    def test_chronology_keeps_exhibit_references_after_sorting_dates(self):
        checksum = "a" * 64
        html = render_packet(
            "Selected correspondence",
            [
                PacketExhibit(
                    "Later response",
                    "I did not go",
                    date(2026, 5, 2),
                    "Contrary material",
                ),
                PacketExhibit(
                    "Undated statement", "I attended", None, "Source excerpt"
                ),
                PacketExhibit(
                    "Earlier plan",
                    "I may attend",
                    date(2026, 5, 1),
                    "Source excerpt",
                    citation="Page 3, first paragraph",
                    source_sha256=checksum,
                ),
            ],
        )
        index, rest = html.split('<section aria-labelledby="packet-chronology">')
        chronology, exhibits = rest.split('<section aria-labelledby="packet-exhibits">')
        self.assertLess(
            index.index('href="#exhibit-1"'), index.index('href="#exhibit-3"')
        )
        self.assertLess(
            chronology.index('href="#exhibit-3"'), chronology.index('href="#exhibit-1"')
        )
        self.assertLess(
            chronology.index('href="#exhibit-1"'), chronology.index('href="#exhibit-2"')
        )
        self.assertIn('<time datetime="2026-05-01">', chronology)
        self.assertIn("Undated", chronology)
        self.assertLess(
            exhibits.index('id="exhibit-1"'), exhibits.index('id="exhibit-3"')
        )
        self.assertIn("Contrary material", exhibits)
        self.assertIn("Page 3, first paragraph", exhibits)
        self.assertEqual(exhibits.count(checksum), 1)

    def test_disclosure_strings_appear_only_as_literal_text(self):
        markup = mark_safe('<img src="https://example.test/private">')
        html = render_packet(
            markup,
            [
                PacketExhibit(
                    markup,
                    f"First line\n[redacted]\n{markup}",
                    None,
                    markup,
                    citation=markup,
                )
            ],
        )

        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertIn('href="#exhibit-1"', html)
        self.assertIn(
            "First line\n[redacted]\n&lt;img src=&quot;https://example.test/private&quot;&gt;",
            html,
        )
        self.assertNotIn("Retained source SHA-256", html)
