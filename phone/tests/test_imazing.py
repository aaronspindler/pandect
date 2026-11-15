"""Synthetic iMazing CSV interpretation, provenance, and publication regressions."""

import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4, uuid5

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, transaction
from django.http import Http404
from django.test import TestCase, TransactionTestCase
from django_tasks_db.models import DBTaskResult

from cases.models import Case, CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.services import validate_output
from core.analysis.sources import Source
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imazing import (
    HEADERS,
    MAX_FIELD_CHARS,
    PARSER_VERSION,
    apply_import,
    iter_import_rows,
    prepare_import,
    read_import_page,
)
from phone.models import CommunicationContext, TextMessageEvidence


def csv_content(*rows, **overrides):
    """Build an 18-column export without any private source material."""
    base = (
        dict.fromkeys(HEADERS, "")
        | {
            "Chat Session": "Synthetic correspondent",
            "Message ID": "synthetic-message-1",
            "Message Date": "2026-07-29 21:23:10",
            "Service": "iMessage",
            "Type": "Incoming",
            "Sender ID": "15555550101",
            "Sender Name": "Synthetic correspondent",
            "Status": "Read",
            "Text": 'Original café, quoted "text".\r\nSecond line.  ',
        }
        | overrides
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, HEADERS)
    writer.writeheader()
    for row in rows or ({},):
        writer.writerow(base | row)
    return stream.getvalue().encode("utf-8")


class IMazingImportTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.correspondent = Person.objects.create(
            case=self.case, first_name="Correspondent"
        )

    def source(self, content=None):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic iMazing export",
            file=ContentFile(content or csv_content(), name="messages.csv"),
        )
        return Source(str(uuid4()), "document", document)

    def preview(self, source):
        parameters = {"phase": "preview", "timezone": "America/Toronto"}
        data = prepare_import([source], parameters)
        run = self.import_run(source, parameters, status=data["outcome"])
        AnalysisResult.objects.create(case=self.case, run=run, data=data)
        return run

    def import_run(self, source, parameters, *, status="queued", actor="owner"):
        return AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users[actor],
            operation="imazing_import",
            method_version="2",
            parameters=parameters,
            inputs=[
                {
                    "id": source.id,
                    "kind": "document",
                    "evidence_id": str(source.item.pk),
                    "sha256": source.item.sha256,
                }
            ],
            status=status,
        )

    def parameters(self, preview, *, create=False):
        return {
            "phase": "import",
            "preview_run_id": str(preview.pk),
            "timezone": "America/Toronto",
            "people": {
                role: {
                    "id": str(uuid5(preview.pk, role) if create else person.pk),
                    "create": {"first_name": f"New {role}"} if create else None,
                }
                for role, person in (
                    ("account", self.person),
                    ("correspondent", self.correspondent),
                )
            },
        }

    def confirmed(self, source, *, create=False, actor="owner"):
        preview = self.preview(source)
        parameters = self.parameters(preview, create=create)
        run = self.import_run(source, parameters, actor=actor)
        return run, prepare_import([source], parameters)

    def publish(self, run, data, source):
        with transaction.atomic():
            return apply_import(run, data, [source])

    def test_preview_retains_exact_decoded_text_spans_and_raw_context(self):
        content = b"\xef\xbb\xbf" + csv_content(
            **{
                "Delivered Date": "2026-07-29 21:24:00",
                "Edited Date": "reported edit",
                "Deleted Date": "reported deletion",
                "Reactions": "Loved by a source account",
                "Subject": "A subject",
                "Attachment": "../../private/image.jpg",
                "Attachment type": "image/jpeg",
                "Replying to": "prior-message",
            }
        )
        source = self.source(content)
        data = prepare_import(
            [source], {"phase": "preview", "timezone": "America/Toronto"}
        )
        self.assertEqual(data["items"], [])
        item = next(iter_import_rows(source, {"timezone": "America/Toronto"}))
        values = item["values"]
        decoded = content.decode("utf-8-sig")
        span = decoded[values["source_start"] : values["source_end"]]
        cells = next(csv.reader(io.StringIO(span, newline="")))
        self.assertEqual(cells[HEADERS.index("Text")], item["text"])
        self.assertEqual(
            item["text"], 'Original café, quoted "text".\r\nSecond line.  '
        )
        self.assertEqual(values["source_row"], 2)
        self.assertEqual(values["occurred_at"], "2026-07-30T01:23:10+00:00")
        self.assertEqual(values["parser_version"], PARSER_VERSION)
        self.assertEqual(values["native_thread_id"], "")
        self.assertEqual(values["reply_to_id"], "prior-message")
        raw = values["source_context"]["raw_fields"]
        self.assertEqual(raw["Reactions"], "Loved by a source account")
        self.assertNotIn("Text", raw)
        self.assertEqual(values["attachments"][0]["href"], "../../private/image.jpg")
        self.assertNotIn("import_message", values)
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(CommunicationContext.objects.exists())
        with source.item.file.open("rb") as original:
            self.assertEqual(original.read(), content)

    def test_incoming_outgoing_publication_and_repeat_are_duplicate_safe(self):
        source = self.source(
            csv_content(
                {},
                {
                    "Type": "Outgoing",
                    "Sender ID": "",
                    "Sender Name": "",
                    "Text": "Reply",
                },
            )
        )
        run, data = self.confirmed(source)
        self.assertEqual(self.publish(run, data, source), 2)
        contexts = list(CommunicationContext.objects.order_by("source_row"))
        self.assertEqual(contexts[0].message.sender, self.correspondent)
        self.assertEqual(contexts[0].message.recipient, self.person)
        self.assertEqual(contexts[1].message.sender, self.person)
        self.assertEqual(contexts[1].message.recipient, self.correspondent)
        self.assertEqual(contexts[1].raw_sender, "Outgoing")
        self.assertEqual(data["import_publication_version"], 2)
        self.assertEqual(data["metrics"]["created_rows"], 2)
        self.assertTrue(all(context.import_run_id == run.pk for context in contexts))
        self.assertEqual(self.publish(run, data, source), 0)
        self.assertEqual(TextMessageEvidence.objects.count(), 2)
        self.assertEqual(CommunicationContext.objects.count(), 2)
        self.assertEqual(data["metrics"]["reused_rows"], 2)

    def test_outgoing_only_unknown_source_labels_allow_reviewed_associations(self):
        source = self.source(
            csv_content(
                **{
                    "Chat Session": "",
                    "Type": "Outgoing",
                    "Sender ID": "",
                    "Sender Name": "",
                }
            )
        )
        run, data = self.confirmed(source)
        self.assertEqual(data["imazing"]["chat_session"], "")
        self.assertEqual(data["imazing"]["sender_id"], "")
        self.assertEqual(data["imazing"]["sender_names"], [])
        self.assertEqual(self.publish(run, data, source), 1)
        self.assertEqual(TextMessageEvidence.objects.get().sender, self.person)

    def test_invalid_rows_are_excluded_while_raw_content_remains_reviewable(self):
        source = self.source(
            csv_content(
                {},
                {"Message Date": "invalid date", "Text": "Still retained"},
                {"Message Date": "2026-11-01 01:30:00"},
                {"Message Date": "2026-03-08 02:30:00"},
                {"Text": "", "Attachment": "https://invalid.example.test/image"},
                {"Type": "System"},
            )
        )
        run, data = self.confirmed(source)
        self.assertEqual(
            data["metrics"], {"source_rows": 6, "ready_rows": 1, "unresolved_rows": 5}
        )
        self.assertEqual(data["outcome"], "partial")
        rows = list(iter_import_rows(source, run.parameters))
        self.assertEqual(rows[1]["text"], "Still retained")
        self.assertTrue(all("unresolved_reason" in item["values"] for item in rows[1:]))
        self.assertEqual(self.publish(run, data, source), 1)
        self.assertEqual(data["metrics"]["unresolved_rows"], 5)

    def test_structurally_unsupported_csv_has_safe_failures(self):
        content = csv_content()
        invalid = (
            (
                content.replace(b"Chat Session", b"Other column", 1),
                "imazing_invalid_format",
            ),
            (content + b"short,row\r\n", "imazing_invalid_format"),
            (content + b'"unfinished', "imazing_invalid_format"),
            (content.replace(b"Original", b"\x00Original"), "imazing_invalid_format"),
            (content + b"\xff", "imazing_invalid_format"),
            (
                csv_content({}, {"Chat Session": "Another session"}),
                "imazing_multiple_chats",
            ),
            (csv_content({}, {"Sender ID": "15555550102"}), "imazing_multiple_chats"),
        )
        for raw, code in invalid:
            with self.subTest(code=code), self.assertRaises(AnalysisError) as raised:
                prepare_import(
                    [self.source(raw)], {"phase": "preview", "timezone": "UTC"}
                )
            self.assertEqual(raised.exception.code, code)

    def test_normal_file_limit_and_per_field_safeguard_remain(self):
        for content, file_limit in (
            (csv_content(Text="x" * 20000), 10000),
            (csv_content(Text="x" * (MAX_FIELD_CHARS + 1)), 1024 * 1024),
        ):
            source = self.source(content)
            with self.assertRaises(AnalysisError) as raised:
                with self.settings(PANDECT_MAX_FILE_BYTES=file_limit):
                    prepare_import([source], {"phase": "preview", "timezone": "UTC"})
            self.assertEqual(raised.exception.code, "imazing_import_limit")

    def test_more_than_ten_thousand_rows_have_a_compact_validated_result(self):
        count = 10001
        source = self.source(
            csv_content(
                *({} for _ in range(count)),
                **{
                    "Type": "Outgoing",
                    "Text": "x",
                    "Message Date": "",
                    "Chat Session": "",
                    "Sender ID": "",
                    "Sender Name": "",
                },
            )
        )
        data = prepare_import([source], {"phase": "preview", "timezone": "UTC"})
        self.assertEqual(data["metrics"]["source_rows"], count)
        self.assertEqual(data["metrics"]["ready_rows"], count)
        self.assertEqual(data["items"], [])
        self.assertLess(len(json.dumps(data).encode()), 65536)
        validate_output(data, [source])

    def test_full_publication_exceeds_previous_row_and_file_caps(self):
        count = 501
        source = self.source(csv_content(*({} for _ in range(count)), Text="x" * 4200))
        self.assertGreater(source.item.byte_size, 2 * 1024 * 1024)
        run, data = self.confirmed(source)
        self.assertEqual(self.publish(run, data, source), count)
        self.assertEqual(
            CommunicationContext.objects.filter(import_run=run).count(), count
        )
        self.assertEqual(TextMessageEvidence.objects.count(), count)
        self.assertEqual(self.publish(run, data, source), 0)
        self.assertEqual(data["metrics"]["reused_rows"], count)

    def test_streamed_unicode_multiline_spans_are_decoded_character_offsets(self):
        bodies = ("📱 café\r\n" * 1500, "Second record é\r\nend")
        content = b"\xef\xbb\xbf" + csv_content(
            {"Text": bodies[0]}, {"Text": bodies[1]}, Subject="é" * 9000
        )
        source = self.source(content)
        rows = list(iter_import_rows(source, {"timezone": "UTC"}))
        decoded = content.decode("utf-8-sig")
        for item, body in zip(rows, bodies, strict=True):
            values = item["values"]
            span = decoded[values["source_start"] : values["source_end"]]
            cells = next(csv.reader(io.StringIO(span, newline="")))
            self.assertEqual(cells[HEADERS.index("Text")], body)
            self.assertEqual(item["text"], body)
        second_start = rows[1]["values"]["source_start"]
        self.assertNotEqual(len(decoded[:second_start].encode("utf-8")), second_start)

    def test_review_page_verifies_source_and_digest_before_returning_bounded_rows(self):
        source = self.source(csv_content(*({} for _ in range(55)), Text="x" * 500))
        parameters = {"phase": "preview", "timezone": "UTC"}
        data = prepare_import([source], parameters)
        parameters |= {
            "_imazing_text_limit_bytes": data["imazing"]["text_limit_bytes"],
            "_imazing_interpretation_sha256": data["imazing"]["interpretation_sha256"],
        }
        rows, total, samples = read_import_page(source, parameters, page=2)
        self.assertEqual(total, 55)
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]["values"]["source_row"], 27)
        self.assertEqual(len(rows[0]["text"]), 500)
        self.assertEqual(len(samples), 3)
        self.assertTrue(all(len(sample["text"]) <= 240 for sample in samples))
        with self.assertRaises(AnalysisError):
            read_import_page(
                source,
                parameters | {"_imazing_interpretation_sha256": "0" * 64},
                page=1,
            )
        with source.item.file.storage.open(source.item.file.name, "wb") as original:
            original.write(csv_content(Text="Changed after review"))
        with self.assertRaises(AnalysisError) as raised:
            read_import_page(source, parameters, page=1)
        self.assertEqual(raised.exception.code, "imazing_source_changed")

    def test_summary_contact_labels_are_bounded_and_raw_rows_remain_complete(self):
        names = [f"Name {number} " + "x" * 300 for number in range(7)]
        source = self.source(csv_content(*({"Sender Name": name} for name in names)))
        data = prepare_import([source], {"timezone": "UTC"})
        summary = data["imazing"]
        self.assertEqual(len(summary["sender_names"]), 5)
        self.assertTrue(summary["sender_names_omitted"])
        self.assertTrue(summary["sender_names_truncated"])
        self.assertTrue(all(len(name) <= 255 for name in summary["sender_names"]))
        first = next(iter_import_rows(source, {"timezone": "UTC"}))
        self.assertEqual(
            first["values"]["account_identifiers"]["sender_name"], names[0]
        )

    def test_timezone_is_explicit_and_blank_message_time_remains_unknown(self):
        source = self.source(csv_content(**{"Message Date": ""}))
        with self.assertRaises(AnalysisError) as raised:
            prepare_import([source], {"phase": "preview"})
        self.assertEqual(raised.exception.code, "imazing_invalid_confirmation")
        run, data = self.confirmed(source)
        self.assertIsNone(
            next(iter_import_rows(source, run.parameters))["values"]["occurred_at"]
        )
        self.assertEqual(self.publish(run, data, source), 1)
        self.assertIsNone(TextMessageEvidence.objects.get().occurred_at)

    def test_configured_text_byte_limit_excludes_only_oversized_message(self):
        source = self.source(csv_content({"Text": "ééé"}, {"Text": "ok"}))
        with self.settings(PANDECT_MAX_TEXT_BYTES=5):
            run, data = self.confirmed(source)
            self.assertEqual(data["metrics"]["ready_rows"], 1)
            row = next(iter_import_rows(source, run.parameters))
            self.assertIn("byte limit", row["values"]["unresolved_reason"])
            self.assertEqual(self.publish(run, data, source), 1)
        self.assertEqual(TextMessageEvidence.objects.get().body, "ok")

    def test_text_limit_drift_cannot_change_reviewed_exclusions_at_publication(self):
        for preview_limit, import_limit in ((5, 50), (50, 5), (50, 60)):
            with self.subTest(preview_limit=preview_limit, import_limit=import_limit):
                source = self.source(csv_content(Text="A reviewed message."))
                with self.settings(PANDECT_MAX_TEXT_BYTES=preview_limit):
                    preview = self.preview(source)
                parameters = self.parameters(preview, create=True)
                run = self.import_run(source, parameters)
                before = Person.objects.count()
                with self.settings(PANDECT_MAX_TEXT_BYTES=import_limit):
                    data = prepare_import([source], parameters)
                    with self.assertRaises(AnalysisError) as raised:
                        self.publish(run, data, source)
                self.assertEqual(raised.exception.code, "imazing_invalid_confirmation")
                self.assertEqual(Person.objects.count(), before)
                self.assertFalse(TextMessageEvidence.objects.exists())
                self.assertFalse(CommunicationContext.objects.exists())

    def test_new_people_are_created_once_and_later_edits_are_preserved(self):
        source = self.source()
        run, data = self.confirmed(source, create=True)
        before = Person.objects.count()
        self.assertEqual(self.publish(run, data, source), 1)
        self.assertEqual(Person.objects.count(), before + 2)
        person_id = run.parameters["people"]["account"]["id"]
        Person.objects.filter(pk=person_id).update(first_name="Reviewed later")
        retry_data = prepare_import([source], run.parameters)
        self.assertEqual(self.publish(run, retry_data, source), 0)
        self.assertEqual(Person.objects.count(), before + 2)
        self.assertEqual(Person.objects.get(pk=person_id).first_name, "Reviewed later")

    def test_publication_failure_rolls_back_people_messages_and_context(self):
        source = self.source(csv_content({}, {"Text": "Fail publication"}))
        run, data = self.confirmed(source, create=True)
        before = Person.objects.count()
        original_save = TextMessageEvidence.save

        def save(message, *args, **kwargs):
            if message.body == "Fail publication":
                raise RuntimeError("Synthetic publication failure")
            return original_save(message, *args, **kwargs)

        with (
            patch.object(TextMessageEvidence, "save", save),
            self.assertRaises(RuntimeError),
        ):
            self.publish(run, data, source)
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(CommunicationContext.objects.exists())

    def test_all_excluded_rows_do_not_create_people(self):
        source = self.source(csv_content(Text=""))
        run, data = self.confirmed(source, create=True)
        before = Person.objects.count()
        self.assertEqual(self.publish(run, data, source), 0)
        self.assertEqual(Person.objects.count(), before)

    def test_changed_mapping_rejects_without_replacing_originals(self):
        source = self.source()
        run, data = self.confirmed(source)
        self.publish(run, data, source)
        parameters = deepcopy(run.parameters)
        parameters["people"]["account"]["id"] = str(self.correspondent.pk)
        changed = self.import_run(source, parameters)
        changed_data = prepare_import([source], parameters)
        with self.assertRaises(AnalysisError) as raised:
            self.publish(changed, changed_data, source)
        self.assertEqual(raised.exception.code, "imazing_conflict")
        self.assertEqual(TextMessageEvidence.objects.count(), 1)
        self.assertEqual(TextMessageEvidence.objects.get().recipient, self.person)

    def test_legacy_context_reuse_preserves_original_provenance_and_membership(self):
        source = self.source()
        run, data = self.confirmed(source)
        self.publish(run, data, source)
        context = CommunicationContext.objects.get()
        # Method 1 contexts predate the additive membership field.
        CommunicationContext.objects.filter(pk=context.pk).update(import_run=None)
        repeated = self.import_run(source, run.parameters)
        repeated_data = prepare_import([source], repeated.parameters)
        self.assertEqual(self.publish(repeated, repeated_data, source), 0)
        context.refresh_from_db()
        self.assertIsNone(context.import_run_id)
        self.assertEqual(context.parser_version, PARSER_VERSION)
        self.assertEqual(repeated_data["metrics"]["reused_rows"], 1)

    def test_import_membership_rejects_crosscase_run_and_survives_run_deletion(self):
        source = self.source()
        run, data = self.confirmed(source)
        self.publish(run, data, source)
        context = CommunicationContext.objects.get()
        foreign = AnalysisRun.objects.create(
            case=self.other_case,
            requested_by=self.users["outsider"],
            operation="imazing_import",
            method_version="2",
            parameters={"phase": "import"},
        )
        context.import_run = foreign
        with self.assertRaises(ValidationError):
            context.clean()
        context.import_run = self.import_run(self.source(), {"phase": "import"})
        with self.assertRaises(ValidationError):
            context.clean()
        run.delete()
        context.refresh_from_db()
        self.assertIsNone(context.import_run_id)
        self.assertEqual(TextMessageEvidence.objects.count(), 1)

    def test_stale_preview_timezone_and_tampered_result_are_rejected(self):
        source = self.source()
        run, data = self.confirmed(source)
        tampered = deepcopy(data)
        tampered["imazing"]["interpretation_sha256"] = "0" * 64
        with self.assertRaises(AnalysisError):
            self.publish(run, tampered, source)
        changed_zone = run.parameters | {"timezone": "UTC"}
        changed_run = self.import_run(source, changed_zone)
        with self.assertRaises(AnalysisError) as raised:
            self.publish(changed_run, prepare_import([source], changed_zone), source)
        self.assertEqual(raised.exception.code, "imazing_invalid_confirmation")
        wrong_preview = run.parameters | {"preview_run_id": str(uuid4())}
        with self.assertRaises(AnalysisError):
            self.publish(self.import_run(source, wrong_preview), data, source)
        self.assertFalse(TextMessageEvidence.objects.exists())

    def test_invalid_or_crosscase_person_choices_and_creation_are_rejected(self):
        source = self.source()
        preview = self.preview(source)
        foreign = Person.objects.create(case=self.other_case, first_name="Foreign")
        parameters = self.parameters(preview)
        parameters["people"]["account"]["id"] = str(foreign.pk)
        with self.assertRaises(AnalysisError):
            prepare_import([source], parameters)
        for create in ({}, {"email": "not-an-email"}, {"notes": "unsupported field"}):
            parameters = self.parameters(preview, create=True)
            parameters["people"]["account"]["create"] = create
            with self.assertRaises(AnalysisError):
                prepare_import([source], parameters)

    def test_revocation_and_viewer_cannot_publish(self):
        source = self.source()
        run, data = self.confirmed(source, actor="editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with self.assertRaises(Http404):
            self.publish(run, data, source)
        viewer = self.import_run(source, run.parameters, actor="viewer")
        with self.assertRaises(PermissionDenied):
            self.publish(viewer, data, source)
        self.assertFalse(TextMessageEvidence.objects.exists())

    def test_checksum_drift_rejects_without_publishing(self):
        source = self.source()
        run, data = self.confirmed(source)
        with source.item.file.storage.open(source.item.file.name, "wb") as original:
            original.write(csv_content(Text="Different retained bytes"))
        with self.assertRaises(AnalysisError) as raised:
            self.publish(run, data, source)
        self.assertEqual(raised.exception.code, "imazing_source_changed")
        self.assertFalse(TextMessageEvidence.objects.exists())

    def test_text_is_not_duplicated_in_result_metadata(self):
        source = self.source(csv_content(Text="A unique synthetic message."))
        run, data = self.confirmed(source)
        self.assertEqual(json.dumps(data).count("A unique synthetic message."), 0)
        self.publish(run, data, source)
        self.assertEqual(json.dumps(data).count("A unique synthetic message."), 0)


class ConcurrentIMazingConfirmationTests(TransactionTestCase):
    def test_parallel_confirmations_queue_one_run_without_creating_people(self):
        from phone.imazing_imports import confirm_import

        owner = get_user_model().objects.create_user(
            username="concurrent-imazing-owner"
        )
        case = Case.objects.create(owner=owner, name="Synthetic concurrent import")
        with TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
            document = DocumentEvidence.objects.create(
                case=case,
                uploaded_by=owner,
                title="Synthetic concurrent messages",
                file=ContentFile(csv_content(), name="synthetic.csv"),
            )
            source = Source(str(uuid4()), "document", document)
            parameters = {"phase": "preview", "timezone": "America/Toronto"}
            data = prepare_import([source], parameters)
            preview = AnalysisRun.objects.create(
                case=case,
                requested_by=owner,
                operation="imazing_import",
                method_version="2",
                parameters=parameters,
                inputs=[
                    {
                        "id": source.id,
                        "kind": "document",
                        "evidence_id": str(document.pk),
                        "sha256": document.sha256,
                    }
                ],
                status="complete",
            )
            AnalysisResult.objects.create(case=case, run=preview, data=data)
            people = {
                role: {
                    "id": str(uuid5(preview.pk, role)),
                    "create": {"first_name": f"Reviewed {role}"},
                }
                for role in ("account", "correspondent")
            }
            barrier = Barrier(2)

            def confirm(_):
                close_old_connections()
                try:
                    actor = get_user_model().objects.get(pk=owner.pk)
                    barrier.wait(timeout=10)
                    run, created = confirm_import(actor, case.pk, preview.pk, people)
                    return run.pk, created
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(confirm, range(2)))
            self.assertEqual(len({identifier for identifier, _ in results}), 1)
            self.assertEqual(sorted(created for _, created in results), [False, True])
            self.assertEqual(
                AnalysisRun.objects.filter(parameters__phase="import").count(), 1
            )
            self.assertEqual(DBTaskResult.objects.count(), 1)
            self.assertFalse(Person.objects.exists())
            self.assertFalse(TextMessageEvidence.objects.exists())
            self.assertFalse(CommunicationContext.objects.exists())
