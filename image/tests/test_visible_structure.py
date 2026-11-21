"""Qualify local OCR layout against generated, spatially annotated originals."""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core.analysis.errors import AnalysisError
from core.analysis.testing import file_source
from image.analysis.ai import ocr
from image.analysis.ai_parser import _ocr
from image.testing import printed_layout


def candidate_fields(result, structure, kind):
    fields = []
    for candidate in structure["candidates"]:
        if candidate["kind"] != kind:
            continue
        values = {}
        for field in candidate["fields"]:
            words = [structure["words"][index] for index in field["words"]]
            values[field["role"]] = " ".join(
                result["items"][word["item"]]["text"][word["start"] : word["end"]]
                for word in words
            )
        fields.append(values)
    return fields


def recognize_lines(*lines, weak_word=None, corrupt_words=False, paragraph_starts=()):
    """Give the parser deterministic TSV in possibly nonvisual engine order."""
    rows = [
        "level page_num block_num par_num line_num word_num "
        "left top width height conf text".replace(" ", "\t")
    ]
    paragraph = 1
    for line, (left, top, phrase) in enumerate(lines, 1):
        if line in paragraph_starts:
            paragraph += 1
        for number, word in enumerate(phrase.split(), 1):
            width = 13 * len(word)
            confidence = 36 if word == weak_word else 96
            rows.append(
                f"5\t1\t1\t{paragraph}\t{line}\t{number}\t{left}\t{top}\t"
                f"{width}\t22\t{confidence}\t{word}"
            )
            left += width + 9
    content, _ = printed_layout("form")
    source = file_source(content, "image")
    with patch(
        "image.analysis.ai_parser._tesseract", side_effect=["test", "\n".join(rows)]
    ):
        prepared = _ocr(content)
    if corrupt_words:
        prepared["regions"][0]["words"][0]["end"] += 1
    with patch("image.analysis.ai.run_parser", return_value=prepared):
        result = ocr([source], {}, authorize=Mock())
    return result, result.get("visible_structure", {}).get(source.id, {})


class VisibleStructureTests(SimpleTestCase):
    def recognize(self, kind):
        content, boxes = printed_layout(kind)
        source = file_source(content, "document" if kind == "page" else "image")
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            result = ocr([source], {}, authorize=Mock())
        gateway.assert_not_called()
        self.assertEqual(source.item.file.storage.content, content)
        structure = result["visible_structure"][source.id]
        readings = {}
        for word in structure["words"]:
            line = result["items"][word["item"]]
            self.assertEqual(line["source_ids"], [source.id])
            self.assertEqual(line["values"]["Input SHA-256"], source.item.sha256)
            self.assertEqual(
                line["anchors"][0]["coordinate_space"], "original_unoriented_pixels"
            )
            text = line["text"][word["start"] : word["end"]]
            readings[text.casefold()] = word["region"]
        for word, expected in boxes.items():
            with self.subTest(layout=kind, word=word):
                actual = readings[word.casefold()]
                # Tesseract sometimes includes padding above or below the ink.
                # Require at least half the union area to cover the same word.
                overlap = max(
                    0, min(actual[2], expected[2]) - max(actual[0], expected[0])
                ) * max(0, min(actual[3], expected[3]) - max(actual[1], expected[1]))
                total_area = sum(
                    (right - left) * (bottom - top)
                    for left, top, right, bottom in (actual, expected)
                )
                self.assertGreaterEqual(overlap / (total_area - overlap), 0.5)
        for candidate in structure["candidates"]:
            for field in candidate["fields"]:
                self.assertEqual(
                    field["confidence"],
                    min(structure["words"][i]["confidence"] for i in field["words"]),
                )
        return result, structure

    def test_chat_preserves_literal_labels_and_message_bodies(self):
        result, structure = self.recognize("chat")
        self.assertEqual(
            candidate_fields(result, structure, "message"),
            [
                {
                    "Participant label": "Morgan",
                    "Time label": "09:15",
                    "Body": "Bring the binder",
                },
                {
                    "Participant label": "Riley",
                    "Time label": "09:16",
                    "Body": "Meet at noon",
                },
            ],
        )

    def test_receipt_associates_amount_with_its_visual_row(self):
        result, structure = self.recognize("receipt")
        self.assertEqual(
            candidate_fields(result, structure, "table_row"),
            [
                {"Cell 1": "Pencils", "Cell 2": "$3.50"},
                {"Cell 1": "Folders", "Cell 2": "$8.25"},
                {"Cell 1": "Paper", "Cell 2": "$2.40"},
            ],
        )

    def test_wrapped_message_keeps_its_words_and_marks_an_uncertain_boundary(self):
        result, structure = recognize_lines(
            (70, 50, "Morgan 09:15"),
            (70, 95, "Bring the binder"),
            (72, 129, "to the lobby"),
            (72, 164, "Side entrance"),
            (270, 235, "Riley 09:16"),
            (270, 278, "See you"),
            paragraph_starts=(4, 5),
        )
        self.assertEqual(result["outcome"], "partial")
        messages = candidate_fields(result, structure, "message")
        self.assertEqual(messages[0]["Body"], "Bring the binder to the lobby")
        self.assertEqual(messages[1]["Body"], "See you")
        self.assertIn(
            {"Text": "Side entrance"}, candidate_fields(result, structure, "block")
        )
        message = structure["candidates"][0]
        self.assertTrue(message["notes"])
        body = next(field for field in message["fields"] if field["role"] == "Body")
        self.assertEqual({structure["words"][i]["item"] for i in body["words"]}, {1, 2})

    def test_intervening_misaligned_cells_keep_words_without_column_assignments(self):
        result, structure = recognize_lines(
            (65, 65, "Pencils"),
            (785, 65, "$3.50"),
            (325, 125, "Other"),
            (545, 125, "context"),
            (65, 185, "Paper"),
            (785, 185, "$2.40"),
        )
        self.assertEqual(result["outcome"], "partial")
        rows = [c for c in structure["candidates"] if c["kind"] == "table_row"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([f["role"] for f in rows[1]["fields"]], ["Visible cell"] * 2)
        self.assertTrue(rows[1]["notes"])
        self.assertEqual(
            [
                result["items"][structure["words"][i]["item"]]["text"]
                for field in rows[1]["fields"]
                for i in field["words"]
            ],
            ["Other", "context"],
        )
        self.assertTrue(all(not row["notes"] for row in (rows[0], rows[2])))

    def test_form_has_independently_anchored_labels_and_values(self):
        result, structure = self.recognize("form")
        self.assertEqual(
            candidate_fields(result, structure, "form_field"),
            [
                {"Label": "Name:", "Value": "River Hall"},
                {"Label": "City:", "Value": "Toronto"},
            ],
        )

    def test_mildly_skewed_photographed_page_preserves_prose_order(self):
        result, structure = self.recognize("page")
        fields = candidate_fields(result, structure, "block")
        self.assertEqual(
            " ".join(block["Text"] for block in fields),
            "Station report Morning trains arrived early. "
            "Passengers used another platform.",
        )

    def test_field_preserves_weak_word_while_label_score_stays_distinct(self):
        result, structure = recognize_lines(
            (80, 80, "City: River Hall"), weak_word="Hall"
        )
        self.assertEqual(result["outcome"], "partial")
        form = next(c for c in structure["candidates"] if c["kind"] == "form_field")
        self.assertEqual(
            {field["role"]: field["confidence"] for field in form["fields"]},
            {"Label": 96, "Value": 36},
        )

    def test_column_major_receipt_preserves_raw_order_and_missing_amount(self):
        lines = (
            (70, 65, "Pencils"),
            (70, 125, "Paper"),
            (70, 185, "Folders"),
            (560, 65, "$3.50"),
            (560, 185, "$8.25"),
        )
        result, structure = recognize_lines(*lines)
        self.assertEqual(
            [item["text"] for item in result["items"]],
            [phrase for _, _, phrase in lines],
        )
        fields = candidate_fields(result, structure, "table_row")
        self.assertIn({"Cell 1": "Pencils", "Cell 2": "$3.50"}, fields)
        self.assertIn({"Cell 1": "Folders", "Cell 2": "$8.25"}, fields)
        self.assertTrue(any(list(row.values()) == ["Paper"] for row in fields))
        self.assertEqual(result["outcome"], "partial")

    def test_two_prices_in_one_cell_remain_unresolved(self):
        result, structure = recognize_lines(
            (70, 65, "Pencils"),
            (560, 65, "$3.50"),
            (70, 125, "Paper"),
            (560, 125, "$2.15 $3.10"),
            (70, 185, "Folders"),
            (560, 185, "$8.25"),
        )
        self.assertEqual(result["outcome"], "partial")
        rows = [
            candidate
            for candidate in structure["candidates"]
            if candidate["kind"] == "table_row"
        ]
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[1]["notes"])
        self.assertIn("$2.15 $3.10", [line["text"] for line in result["items"]])

    def test_cropped_repeated_form_labels_keep_conflicting_observations(self):
        result, structure = recognize_lines(
            (0, 1, "City: River"), (0, 70, "City: Lake")
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            [line["text"] for line in result["items"]], ["City: River", "City: Lake"]
        )
        self.assertTrue(
            structure["notes"] or any(c["notes"] for c in structure["candidates"])
        )
        self.assertEqual(
            candidate_fields(result, structure, "form_field"),
            [{"Label": "City:", "Value": "River"}, {"Label": "City:", "Value": "Lake"}],
        )

    def test_numbers_in_prose_columns_preserve_alternative_orders(self):
        result, structure = recognize_lines(
            (60, 80, "Morning trains arrive"),
            (60, 130, "Residents walk home"),
            (540, 80, "Platform 3 opens"),
            (540, 130, "Evening service ended"),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertGreaterEqual(len(structure["orders"]), 2)
        first, second = (order["candidates"] for order in structure["orders"][:2])
        self.assertCountEqual(first, second)
        self.assertNotEqual(first, second)

    def test_isolated_time_and_reply_decoration_do_not_make_a_message(self):
        result, structure = recognize_lines(
            (65, 70, "Morgan 09:15"), (65, 125, "Reply")
        )
        self.assertEqual(candidate_fields(result, structure, "message"), [])

    def test_contradictory_chat_time_labels_leave_body_unassigned(self):
        result, structure = recognize_lines(
            (70, 60, "Morgan 09:15 11:30"),
            (70, 102, "Unattributed wording"),
            (150, 185, "Riley 09:16"),
            (150, 227, "Meet at noon"),
        )
        self.assertEqual(result["outcome"], "partial")
        messages = candidate_fields(result, structure, "message")
        self.assertIn(
            {"Participant label": "Morgan", "Time label": "09:15 11:30"}, messages
        )
        self.assertIn(
            {
                "Participant label": "Riley",
                "Time label": "09:16",
                "Body": "Meet at noon",
            },
            messages,
        )
        self.assertTrue(
            any(line["text"] == "Unattributed wording" for line in result["items"])
        )

    def test_layout_limits_preserve_lines_and_disclose_incomplete_structure(self):
        for bound in (
            "image.analysis.ai_parser.MAX_LAYOUT_WORDS",
            "image.analysis.ai.MAX_STRUCTURE_BYTES",
        ):
            with self.subTest(bound=bound), patch(bound, 2):
                result, structure = recognize_lines((60, 80, "City: River Hall"))
                self.assertEqual(result["outcome"], "partial")
                self.assertEqual(result["items"][0]["text"], "City: River Hall")
                self.assertFalse(structure["candidates"])
                self.assertTrue(
                    any("limit" in note.lower() for note in structure["notes"])
                )

    def test_invalid_word_span_fails_before_publication(self):
        with self.assertRaisesMessage(AnalysisError, "ocr_invalid_output"):
            recognize_lines((65, 70, "City: Toronto"), corrupt_words=True)
