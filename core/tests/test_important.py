"""Case-scoped marks and attributed notes on retained evidence and findings."""

from uuid import uuid4

from allauth.account.models import EmailAddress
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import process_run, start_run
from core.models import AnalysisResult, ImportantItem, ImportantNote
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class ImportantItemTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.source = self.text()
        self.login("owner")

    def important_url(self, name, **kwargs):
        return reverse(
            f"core:important:{name}", kwargs={"case_id": self.case.pk, **kwargs}
        )

    def mark_url(self, **kwargs):
        return self.important_url(
            "mark_evidence", **{"kind": "text", "evidence_id": self.source.pk, **kwargs}
        )

    def mark_evidence(self):
        self.assertEqual(
            self.client.post(self.mark_url(), {"action": "mark"}).status_code, 302
        )
        return ImportantItem.objects.get(case=self.case, evidence_id=self.source.pk)

    def add_note(self, saved, body):
        return self.client.post(
            self.important_url("notes", item_id=saved.pk), {"body": body}
        )

    def run_entities(self, source, *, previous_run=None):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text", "id": source.pk}],
            previous_run=previous_run,
        )
        process_run(run.pk)
        return run, AnalysisResult.objects.get(run=run)

    def test_shared_notes_stay_attributed_and_readable_after_unmarking(self):
        original = self.url("detail", self.source)
        index = self.important_url("list")
        self.assertNotContains(self.client.get(index), self.source.title)
        self.assertContains(self.client.get(original), "Mark important")
        self.login("editor")
        saved = self.mark_evidence()
        marked_at = saved.marked_at
        self.assertEqual(saved.marked_by, self.users["editor"])
        self.assertEqual(self.mark_evidence().marked_at, marked_at)
        self.assertContains(self.client.get(index), self.source.title)

        editor_body = "Compare with the retained source. <b>Quoted label</b>"
        self.assertEqual(self.add_note(saved, editor_body).status_code, 302)
        self.login("owner")
        owner_body = "Review the sequence next."
        self.assertEqual(self.add_note(saved, owner_body).status_code, 302)
        self.assertCountEqual(
            ImportantNote.objects.filter(item=saved).values_list("author_id", "body"),
            [
                (self.users["editor"].pk, editor_body),
                (self.users["owner"].pk, owner_body),
            ],
        )

        self.assertEqual(
            self.client.post(self.mark_url(), {"action": "unmark"}).status_code, 302
        )
        self.assertNotContains(self.client.get(index), self.source.title)
        notes = self.important_url("notes", item_id=saved.pk)
        self.assertContains(self.client.get(original), notes)
        self.assertEqual(
            self.add_note(saved, "Cannot append while unmarked").status_code, 403
        )
        self.login("viewer")
        response = self.client.get(notes)
        self.assertContains(response, "&lt;b&gt;Quoted label&lt;/b&gt;")
        self.assertContains(response, owner_body)
        self.assertNotContains(response, 'name="body"')

        self.login("owner")
        self.assertEqual(self.mark_evidence().pk, saved.pk)
        self.assertContains(self.client.get(index), self.source.title)
        self.assertEqual(ImportantNote.objects.filter(item=saved).count(), 2)

    def test_current_case_permissions_and_post_only_marks(self):
        saved = self.mark_evidence()
        index = self.important_url("list")
        notes = self.important_url("notes", item_id=saved.pk)
        self.assertEqual(self.client.get(self.mark_url()).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users["owner"])
        self.assertEqual(
            client.post(self.mark_url(), {"action": "unmark"}).status_code, 403
        )
        self.assertEqual(
            client.post(notes, {"body": "Unverified submission"}).status_code, 403
        )

        self.login("viewer")
        self.assertContains(self.client.get(index), self.source.title)
        self.assertEqual(self.client.get(notes).status_code, 200)
        self.assertEqual(self.add_note(saved, "Viewer submission").status_code, 403)
        self.assertEqual(
            self.client.post(self.mark_url(), {"action": "unmark"}).status_code, 403
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        for role in ("editor", "outsider"):
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.client.get(index).status_code, 404)
                self.assertEqual(self.client.get(notes).status_code, 404)
                self.assertEqual(
                    self.add_note(saved, "Cross-case submission").status_code, 404
                )
                self.assertEqual(
                    self.client.post(self.mark_url(), {"action": "unmark"}).status_code,
                    404,
                )

        foreign_source = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Foreign source",
            body="Synthetic foreign content",
        )
        self.assertEqual(
            self.client.post(
                self.mark_url(case_id=self.other_case.pk), {"action": "mark"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                self.important_url(
                    "notes", case_id=self.other_case.pk, item_id=saved.pk
                )
            ).status_code,
            404,
        )
        self.login("owner")
        self.assertEqual(
            self.client.post(
                self.mark_url(evidence_id=foreign_source.pk), {"action": "mark"}
            ).status_code,
            404,
        )
        self.assertEqual(ImportantItem.objects.count(), 1)
        self.assertFalse(ImportantNote.objects.exists())

    def test_unverified_account_cannot_read_or_change_shared_items(self):
        saved = self.mark_evidence()
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        index = self.important_url("list")
        notes = self.important_url("notes", item_id=saved.pk)
        verify = reverse("account_email")
        self.assertRedirects(
            self.client.get(index), verify, fetch_redirect_response=False
        )
        self.assertRedirects(
            self.client.get(notes), verify, fetch_redirect_response=False
        )
        self.assertRedirects(
            self.add_note(saved, "Blocked until verification."),
            verify,
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.post(self.mark_url(), {"action": "unmark"}),
            verify,
            fetch_redirect_response=False,
        )
        self.assertFalse(ImportantNote.objects.filter(item=saved).exists())
        saved.refresh_from_db()
        self.assertIsNotNone(saved.marked_at)

    def test_archive_preserves_mark_and_note_validation_preserves_form(self):
        saved = self.mark_evidence()
        response = self.client.post(self.url("archive", self.source), {"confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.source.refresh_from_db()
        self.assertTrue(self.source.is_archived)
        response = self.client.get(self.important_url("list"))
        self.assertContains(response, self.source.title)
        self.assertContains(response, "Archived")
        for body in ("\n \t", "Evidence " * 501):
            with self.subTest(length=len(body)):
                response = self.add_note(saved, body)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"]["body"].errors)
        self.assertFalse(ImportantNote.objects.filter(item=saved).exists())

    def test_short_finding_value_retains_its_candidate_label(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Invoice correspondence",
            body="The invoice says 04/03/26.",
        )
        run, result = self.run_entities(source)
        item_index = next(
            index
            for index, item in enumerate(result.data["items"])
            if item["label"] == "Date candidate"
        )
        mark_url = self.important_url(
            "mark_finding", run_id=run.pk, item_index=item_index
        )
        self.assertEqual(
            self.client.post(mark_url, {"action": "mark"}).status_code, 302
        )
        saved = ImportantItem.objects.get(finding_result=result, item_index=item_index)
        for url in (
            self.important_url("list"),
            self.important_url("notes", item_id=saved.pk),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, "04/03/26")
                self.assertContains(response, "Date candidate")

    def test_exact_finding_survives_rerun_and_withholds_unavailable_content(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Archived correspondence",
            body="Send to first@example.test or second@example.test.",
        )
        run, result = self.run_entities(source)
        mark_url = self.important_url("mark_finding", run_id=run.pk, item_index=1)
        self.assertEqual(
            self.client.post(mark_url, {"action": "mark"}).status_code, 302
        )
        saved = ImportantItem.objects.get(finding_result=result, item_index=1)
        self.assertEqual(
            self.add_note(saved, "Check the second address.").status_code, 302
        )
        self.assertEqual(
            self.client.post(
                self.important_url("mark_finding", run_id=run.pk, item_index=999),
                {"action": "mark"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                self.important_url("mark_finding", run_id=uuid4(), item_index=1),
                {"action": "mark"},
            ).status_code,
            404,
        )
        rerun, rerun_result = self.run_entities(source, previous_run=run)
        self.assertNotEqual(result.pk, rerun_result.pk)
        index = self.important_url("list")
        response = self.client.get(index)
        self.assertContains(response, f"/{run.pk}/?", count=1)
        self.assertContains(response, "#finding-2")
        self.assertNotContains(response, f"/{rerun.pk}/?")

        # Simulate a retained snapshot that no longer matches its source.
        TextEvidence.objects.filter(pk=source.pk).update(sha256="a" * 64)
        response = self.client.get(index)
        self.assertNotContains(response, "second@example.test")
        self.assertNotContains(response, f"/{run.pk}/?")
        self.assertEqual(
            self.client.get(self.important_url("notes", item_id=saved.pk)).status_code,
            404,
        )
        self.assertEqual(self.add_note(saved, "No longer available.").status_code, 404)
        self.assertTrue(ImportantNote.objects.filter(item=saved).exists())
