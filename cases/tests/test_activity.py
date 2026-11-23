from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cases.models import CaseActivity, CaseMembership
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch, ExtractedPassage
from core.testing import EvidenceViewTestMixin
from image.models import ImageEvidence
from people.models import Person
from socialmedia.models import SocialAccount


class CaseActivityTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.activity_url = reverse("cases:activity", args=[self.case.pk])
        self.login("viewer")

    def snapshot(self):
        response = self.client.get(self.activity_url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        return response.json()

    def test_status_sees_a_short_job_and_counts_only_this_cases_active_checks(self):
        before = self.snapshot()
        self.assertFalse(before["active"])
        run = AnalysisRun.objects.create(
            case=self.case, requested_by=self.users["editor"], operation="entities"
        )
        AnalysisRun.objects.create(
            case=self.other_case,
            requested_by=self.users["outsider"],
            operation="entities",
        )
        self.assertEqual(self.snapshot()["queued"], 1)
        AnalysisRun.objects.filter(pk=run.pk).update(status="running")
        during = self.snapshot()
        self.assertTrue(during["active"])
        self.assertEqual((during["queued"], during["running"]), (0, 1))
        AnalysisRun.objects.filter(pk=run.pk).update(status="failed")
        after = self.snapshot()
        self.assertFalse(after["active"])
        self.assertNotEqual(before["versions"]["work"], after["versions"]["work"])
        self.assertEqual(after["versions"]["findings"], before["versions"]["findings"])
        CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"]
        )
        preparing = self.snapshot()
        self.assertTrue(preparing["active"])
        self.assertEqual((preparing["queued"], preparing["running"]), (0, 0))

    def test_bulk_writes_deletes_scope_and_rollback(self):
        self.text()
        baseline = self.snapshot()["versions"]
        with transaction.atomic():
            Person.objects.bulk_create(
                [Person(case=self.case, first_name=f"Name {i}") for i in range(10)]
            )
            changed = self.snapshot()["versions"]
            self.assertEqual(changed, baseline | {"people": baseline["people"] + 1})
            transaction.set_rollback(True)
        Person.objects.create(case=self.other_case, first_name="Another person")
        self.assertEqual(self.snapshot()["versions"], baseline)
        account = SocialAccount.objects.create(
            person=self.person, platform="Example", username="one"
        )
        account.delete()
        self.assertEqual(self.snapshot()["versions"]["people"], baseline["people"] + 2)
        self.person.first_name = "Updated"
        self.person.save()
        updated = self.snapshot()["versions"]
        for key in ("evidence", "findings", "people"):
            self.assertGreater(updated[key], baseline[key])
        self.assertEqual(updated["images"], baseline["images"])
        self.case.delete()
        self.assertFalse(CaseActivity.objects.exclude(case=self.other_case).exists())

    def test_image_extractions_only_advance_the_image_list_for_images(self):
        baseline = self.snapshot()["versions"]
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Test photo",
            file=ContentFile(b"synthetic", name="photo.bin"),
        )
        run = AnalysisRun.objects.create(
            case=self.case, requested_by=self.users["owner"], operation="ocr"
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        published = self.snapshot()["versions"]
        for key in ("work", "evidence", "images", "findings"):
            self.assertGreater(published[key], baseline[key])
        values = dict(
            case=self.case,
            result=result,
            evidence_sha256=image.sha256,
            evidence_id=image.pk,
            text="sample",
        )
        ExtractedPassage.objects.bulk_create(
            [ExtractedPassage(kind="document", **values)]
        )
        other = self.snapshot()["versions"]
        self.assertGreater(other["evidence"], published["evidence"])
        self.assertEqual(other["images"], published["images"])
        ExtractedPassage.objects.bulk_create([ExtractedPassage(kind="image", **values)])
        self.assertGreater(self.snapshot()["versions"]["images"], other["images"])

    def test_baseline_access_and_bounded_idle_query(self):
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            page = self.client.get(reverse("people:list", args=[self.case.pk]))
            with CaptureQueriesContext(connection) as queries:
                snapshot = self.snapshot()
            self.assertEqual(
                snapshot["versions"], page.context["case_activity_versions"]
            )
            self.assertLessEqual(len(queries), 7)
            sql = " ".join(query["sql"] for query in queries)
            for table in (
                "core_analysisresult",
                "core_analysisfinding",
                "phone_textmessageevidence",
            ):
                self.assertNotIn(table, sql)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.activity_url).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.activity_url).status_code, 404)
        self.client.logout()
        self.assertEqual(
            self.client.get(
                self.activity_url, HTTP_ACCEPT="application/json"
            ).status_code,
            401,
        )
