from datetime import UTC, datetime, timedelta

from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
from people.models import Person
from phone.models import TextMessageEvidence


class ChronologyTests(EvidenceViewTestMixin, TestCase):
    def chronology_url(self):
        return reverse("core:evidence:chronology", args=[self.case.pk])

    def message(self, **changes):
        return TextMessageEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Message event",
                    "body": "Synthetic original",
                    "sender": self.person,
                    "recipient": self.person,
                }
                | changes
            )
        )

    def location(self, model=LocationEvidence, **changes):
        extra = (
            {"person": self.person}
            if model is PersonLocationEvidence
            else ({"vehicle": self.vehicle} if model is VehicleLocationEvidence else {})
        )
        return model.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Location event",
                    "address": "Synthetic office",
                    **extra,
                }
                | changes
            )
        )

    def test_observed_and_acquired_are_distinct_and_missing_times_not_guessed(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        message = self.message(
            occurred_at=instant, collected_at=instant + timedelta(days=2)
        )
        location = self.location(observed_at=instant + timedelta(hours=1))
        self.message(title="Undated message")
        self.login("viewer")
        response = self.client.get(self.chronology_url())
        self.assertEqual(
            [(r["id"], r["event_label"]) for r in response.context["rows"]],
            [
                (message.pk, "Occurred"),
                (location.pk, "Observed"),
            ],
        )
        response = self.client.get(self.chronology_url(), {"category": "acquired"})
        self.assertEqual(
            [(r["id"], r["event_label"]) for r in response.context["rows"]],
            [(message.pk, "Collected")],
        )
        self.assertNotContains(response, "Undated message")
        response = self.client.get(self.chronology_url(), {"category": "all"})
        self.assertEqual(len(response.context["rows"]), 3)

    def test_date_range_uses_user_timezone_and_keeps_recorded_utc(self):
        user = self.users["owner"]
        user.timezone = "America/Toronto"
        user.save()
        before = self.message(
            title="Previous local day",
            occurred_at=datetime(2026, 11, 10, 3, tzinfo=UTC),
        )
        inside = self.message(
            title="Requested local day",
            occurred_at=datetime(2026, 11, 10, 6, tzinfo=UTC),
        )
        self.login("owner")
        filters = {"start": "2026-11-10", "end": "2026-11-10"}
        response = self.client.get(self.chronology_url(), filters)
        self.assertEqual([r["id"] for r in response.context["rows"]], [inside.pk])
        self.assertNotContains(response, before.title)
        self.assertContains(response, "America/Toronto")
        self.assertContains(response, "10 Nov 2026 01:00:00 EST")
        self.assertNotContains(response, "10 Nov 2026 06:00:00 UTC")
        response = self.client.get(self.chronology_url(), filters | {"clock": "utc"})
        self.assertEqual([r["id"] for r in response.context["rows"]], [inside.pk])
        self.assertContains(response, "10 Nov 2026 06:00:00 UTC")
        self.assertNotContains(response, "10 Nov 2026 01:00:00 EST")

    def test_date_range_keeps_local_date_after_a_midnight_offset_change(self):
        owner = self.users["owner"]
        owner.timezone = "America/Goose_Bay"
        owner.save(update_fields=["timezone"])
        early = self.message(occurred_at=datetime(1988, 10, 30, 2, tzinfo=UTC))
        self.message(occurred_at=datetime(1988, 10, 30, 2, 1, tzinfo=UTC))
        late = self.message(occurred_at=datetime(1988, 10, 30, 4, tzinfo=UTC))
        self.login("owner")
        response = self.client.get(
            self.chronology_url(), {"start": "1988-10-30", "end": "1988-10-30"}
        )
        self.assertEqual(
            [row["id"] for row in response.context["rows"]], [early.pk, late.pk]
        )

    def test_person_vehicle_and_source_filters_are_case_scoped(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        message = self.message(occurred_at=instant)
        person_location = self.location(PersonLocationEvidence, observed_at=instant)
        vehicle_location = self.location(VehicleLocationEvidence, observed_at=instant)
        self.login("owner")
        response = self.client.get(self.chronology_url(), {"person": self.person.pk})
        self.assertEqual(
            {r["id"] for r in response.context["rows"]},
            {message.pk, person_location.pk},
        )
        response = self.client.get(self.chronology_url(), {"vehicle": self.vehicle.pk})
        self.assertEqual(
            [r["id"] for r in response.context["rows"]], [vehicle_location.pk]
        )
        response = self.client.get(self.chronology_url(), {"type": "text-message"})
        self.assertEqual([r["id"] for r in response.context["rows"]], [message.pk])
        foreign = Person.objects.create(case=self.other_case, first_name="Foreign")
        response = self.client.get(self.chronology_url(), {"person": foreign.pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["rows"], [])
        TextMessageEvidence.objects.filter(pk=message.pk).update(recipient=foreign)
        response = self.client.get(self.chronology_url())
        self.assertNotIn(message.pk, {r["id"] for r in response.context["rows"]})

    def test_invalid_dates_and_revoked_access(self):
        self.login("viewer")
        for params in (
            {"start": "invalid"},
            {"start": "2026-11-11", "end": "2026-11-10"},
        ):
            self.assertEqual(
                self.client.get(self.chronology_url(), params).status_code, 400
            )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.chronology_url()).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.chronology_url()).status_code, 404)

    def test_pagination_excludes_archived_timestamps(self):
        instant = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for index in range(27):
            self.message(
                occurred_at=instant + timedelta(minutes=index),
                title=f"Message {index:02}",
            )
        TextMessageEvidence.objects.filter(title="Message 26").update(
            is_archived=True,
            archive_changed_at=instant,
            archive_changed_by=self.users["owner"],
        )
        self.login("viewer")
        response = self.client.get(self.chronology_url(), {"page": 2})
        self.assertEqual(response.context["page_obj"].paginator.count, 26)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertContains(response, "Message 25")
        self.assertNotContains(response, "Message 26")
        self.assertContains(response, '?view=references"')

    def test_ambiguous_date_strings_remain_unplaced_and_source_linked(self):
        message = self.message(body="Proposed for 11/12/2026; never confirmed.")
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text-message", "id": str(message.pk)}],
        )
        process_run(run.pk)
        self.login("owner")
        response = self.client.get(
            self.chronology_url(), {"start": "2027-01-01", "person": self.person.pk}
        )
        self.assertEqual(response.context["rows"], [])
        self.assertNotContains(response, "11/12/2026")
        self.assertContains(response, "Date ranges apply to recorded timestamps")
        self.assertContains(response, "Extracted date references")
        response = self.client.get(
            self.chronology_url(),
            {"view": "references", "start": "2027-01-01", "person": self.person.pk},
        )
        self.assertEqual(response.context["claims"].paginator.count, 1)
        self.assertContains(response, "11/12/2026")
        self.assertNotContains(response, 'name="start"')

    def test_unplaced_originals_are_scoped_and_paginate_independently(self):
        self.login("viewer")
        collected = datetime(2026, 11, 10, 12, tzinfo=UTC)
        for i in range(26):
            self.message(title=f"Undated {i:02}", collected_at=collected)
        archived = self.message(title="Z archived", body="The appointment is tomorrow.")
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text-message", "id": str(archived.pk)}],
        )
        process_run(run.pk)
        TextMessageEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=collected,
            archive_changed_by=self.users["owner"],
        )
        self.message(title="Dated communication", occurred_at=collected)
        self.location(title="Unconnected observation")
        invalid = self.message(title="Inconsistent communication")
        foreign = Person.objects.create(case=self.other_case, first_name="Foreign")
        TextMessageEvidence.objects.filter(pk=invalid.pk).update(recipient=foreign)
        query = {
            "view": "unplaced",
            "person": self.person.pk,
            "originals_page": 2,
            "start": "2099-01-01",
        }
        response = self.client.get(self.chronology_url(), query)
        self.assertEqual(response.context["originals"].paginator.count, 26)
        self.assertEqual(response.context["claims"].paginator.count, 0)
        self.assertContains(response, "Undated 25")
        self.assertNotContains(response, "Z archived")
        self.assertNotContains(response, "tomorrow")
        self.assertNotContains(response, "Dated communication")
        self.assertNotContains(response, "Unconnected observation")
        self.assertNotContains(response, "Inconsistent communication")
        self.assertNotContains(response, 'name="start"')
        self.assertEqual(
            self.client.get(
                self.chronology_url(), query | {"person": foreign.pk}
            ).status_code,
            400,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.chronology_url(), query).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.chronology_url(), query).status_code, 404)
