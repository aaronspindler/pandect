"""Retain records whose deletion would affect another case."""

from django.db import DEFAULT_DB_ALIAS
from django.test import TestCase
from django.urls import reverse

from cases.deletion import CASCADE_CONFLICT, collected_querysets, outside_case
from cases.models import Case
from core.models import AnalysisRun, CaseAnalysisBatch, PendingFileDeletion
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleTripState
from people.models import Person
from phone.models import ConversationGrouping
from vehicle.models import Vehicle


class CaseDeletionIsolationTests(EvidenceViewTestMixin, TestCase):
    def test_collected_checks_bound_batches_and_keep_querysets_lazy(self):
        people = Person.objects.bulk_create(
            Person(case=self.case, first_name=f"Synthetic person {index}")
            for index in range(500)
        )
        other = Person.objects.create(case=self.other_case, first_name="Other person")
        batches = list(collected_querysets(Person, [*people, other], DEFAULT_DB_ALIAS))
        self.assertEqual([rows.count() for rows in batches], [500, 1])
        self.assertFalse(outside_case(batches[0], self.case.pk))
        self.assertTrue(outside_case(batches[1], self.case.pk))

        existing = Person.objects.filter(case=self.case)
        self.assertIs(
            next(collected_querysets(Person, existing, DEFAULT_DB_ALIAS)), existing
        )

    def test_case_deletion_does_not_clear_another_cases_run(self):
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"]
        )
        retained = AnalysisRun.objects.create(
            case=self.other_case,
            requested_by=self.users["outsider"],
            operation="sessions",
            method_version="1",
        )
        AnalysisRun.objects.filter(pk=retained.pk).update(batch=batch)
        self.login("owner")
        response = self.client.post(
            reverse("cases:case_delete", args=(self.case.pk,)), {"confirm": "on"}
        )
        self.assertContains(response, CASCADE_CONFLICT, status_code=409)
        self.assertNotContains(response, str(retained.pk), status_code=409)
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        retained.refresh_from_db()
        self.assertEqual(retained.batch_id, batch.pk)

    def test_case_deletion_does_not_remove_another_cases_file(self):
        original, retained = (
            DocumentEvidence.objects.create(
                case=case,
                uploaded_by=case.owner,
                title="Retained document",
                file=self.payload("document")["file"],
            )
            for case in (self.case, self.other_case)
        )
        DocumentEvidence.objects.filter(pk=original.pk).update(file=retained.file.name)
        self.login("owner")
        response = self.client.post(
            reverse("cases:case_delete", args=(self.case.pk,)), {"confirm": "on"}
        )
        self.assertContains(response, CASCADE_CONFLICT, status_code=409)
        self.assertTrue(DocumentEvidence.objects.filter(pk=original.pk).exists())
        self.assertTrue(retained.file.storage.exists(retained.file.name))
        self.assertFalse(PendingFileDeletion.objects.exists())

    def test_person_deletion_preserves_another_cases_grouping(self):
        foreign_person = Person.objects.create(case=self.other_case, first_name="Other")
        batch = CaseAnalysisBatch.objects.create(
            case=self.other_case, requested_by=self.users["outsider"]
        )
        retained = ConversationGrouping.objects.create(
            case=self.other_case,
            batch=batch,
            first_person=foreign_person,
            second_person=foreign_person,
            source_count=1,
        )
        first, second = sorted((self.person.pk, foreign_person.pk))
        ConversationGrouping.objects.filter(pk=retained.pk).update(
            first_person_id=first, second_person_id=second
        )
        self.login("owner")
        response = self.client.post(
            reverse("people:delete", args=(self.case.pk, self.person.pk)),
            {"confirm": "on"},
        )
        self.assertContains(response, CASCADE_CONFLICT, status_code=400)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(ConversationGrouping.objects.filter(pk=retained.pk).exists())

    def test_vehicle_deletion_and_admin_reviews_preserve_foreign_cascades(self):
        foreign_vehicle = Vehicle.objects.create(case=self.other_case, model="Other")
        retained = VehicleTripState.objects.create(
            case=self.other_case, vehicle=foreign_vehicle
        )
        VehicleTripState.objects.filter(pk=retained.pk).update(vehicle=self.vehicle)
        self.login("owner")
        response = self.client.post(
            reverse("vehicle:delete", args=(self.case.pk, self.vehicle.pk)),
            {"confirm": "on"},
        )
        self.assertContains(response, CASCADE_CONFLICT, status_code=400)
        owner = self.users["owner"]
        owner.is_staff = owner.is_superuser = True
        owner.save(update_fields=("is_staff", "is_superuser"))
        self.login("owner")
        for route, obj in (
            ("admin:cases_case_delete", self.case),
            ("admin:vehicle_vehicle_delete", self.vehicle),
        ):
            url = reverse(route, args=(obj.pk,))
            for response in (
                self.client.get(url),
                self.client.post(url, {"post": "yes"}),
            ):
                self.assertContains(response, CASCADE_CONFLICT)
                self.assertNotContains(response, str(retained.pk))
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        self.assertTrue(VehicleTripState.objects.filter(pk=retained.pk).exists())
