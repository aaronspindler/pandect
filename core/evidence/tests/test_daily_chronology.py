from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import process_run, start_run
from core.testing import EvidenceViewTestMixin
from geo.daily_chronology import format_trips
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips, saved_trips
from people.models import Person
from phone.models import PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


class DailyChronologyTests(EvidenceViewTestMixin, TestCase):
    def day(self, day=None, **params):
        query = {"view": "day", **params}
        if day is not None:
            query["day"] = day
        return self.client.get(self.url("chronology"), query, follow=True)

    def test_mixed_day_orders_saved_groups_calls_sightings_and_collections(self):
        start = datetime(2026, 6, 3, 8, tzinfo=UTC)
        common = {"case": self.case, "uploaded_by": self.case.owner}
        other = Person.objects.create(case=self.case, first_name="Blair")
        parties = {"sender": self.person, "recipient": other}
        messages = [
            TextMessageEvidence.objects.create(
                **common,
                **parties,
                title=f"Message {minute}",
                body=f"Synthetic message at minute {minute}",
                occurred_at=start + timedelta(minutes=minute),
            )
            for minute in (0, 5, 180, 185, 210)
        ]
        run, _ = start_run(
            self.case.owner,
            self.case.pk,
            "sessions",
            [{"kind": "text-message", "id": str(item.pk)} for item in messages[:4]],
            parameters={"gap_minutes": 60},
        )
        process_run(run.pk)
        sessions = list(run.result.findings.order_by("item_index"))
        self.assertEqual(len(sessions), 2)
        for minute, address in ((30, "2 Market Street"), (40, "6 Harbour Road")):
            VehicleLocationEvidence.objects.create(
                **common,
                vehicle=self.vehicle,
                title="Vehicle position",
                address=address,
                observed_at=start + timedelta(minutes=minute),
            )
        (generation,) = prepare_case_trips(self.case.owner, self.case.pk)
        for _ in range(2):
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        trip = saved_trips(self.case, state="all").get()
        sighting = PersonLocationEvidence.objects.create(
            **common,
            person=other,
            title="Platform sighting",
            address="North platform",
            observed_at=start + timedelta(hours=1),
            collected_at=start + timedelta(hours=5),
        )
        call = PhoneCallEvidence.objects.create(
            **common,
            **parties,
            title="Reported call",
            occurred_at=start + timedelta(hours=4),
            duration=timedelta(minutes=2),
        )
        collected = self.text(collected_at=start + timedelta(hours=6))
        self.login("viewer")
        response = self.day("2026-06-03")
        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(
            [row["id"] for row in rows],
            [
                sessions[0].pk,
                trip.pk,
                sighting.pk,
                sessions[1].pk,
                messages[-1].pk,
                call.pk,
                sighting.pk,
                collected.pk,
            ],
        )
        self.assertContains(response, "Collected")
        self.assertContains(response, "North platform")
        self.assertContains(response, "Blair")
        for row in rows:
            with self.subTest(row=row["id"]):
                self.assertEqual(
                    self.client.get(row["url"], follow=True).status_code, 200
                )
        self.assertIn(f"instance={sessions[1].pk}", rows[3]["url"])
        self.assertEqual(
            rows[1]["url"], reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        )

    def test_day_boundaries_use_local_midnights_while_clock_switches_independently(
        self,
    ):
        self.users["owner"].timezone = "America/Toronto"
        self.users["owner"].save(update_fields=["timezone"])
        # Toronto's fall-back day contains both 01:30 readings and ends 25 hours
        # after its first local midnight.
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        instants = [
            start - timedelta(seconds=1),
            start + timedelta(minutes=90),
            start + timedelta(minutes=150),
            start + timedelta(hours=25, seconds=-1),
            start + timedelta(hours=25),
        ]
        sources = [self.text(collected_at=instant) for instant in instants]
        self.login("owner")
        for clock in ("local", "utc"):
            with self.subTest(clock=clock):
                response = self.day("2026-11-01", clock=clock)
                self.assertEqual(
                    [row["id"] for row in response.context["rows"]],
                    [source.pk for source in sources[1:4]],
                )
                self.assertContains(response, 'value="2026-11-01"')

    def test_latest_and_adjacent_days_exclude_undated_and_foreign_evidence(self):
        start = datetime(2026, 7, 3, 12, tzinfo=UTC)
        self.text(collected_at=start)
        latest = self.text(collected_at=start + timedelta(days=5))
        self.text()
        TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.other_case.owner,
            title="Other collection",
            body="Foreign original",
            collected_at=start + timedelta(days=12),
        )
        self.login("editor")
        response = self.day()
        self.assertEqual([row["id"] for row in response.context["rows"]], [latest.pk])
        self.assertEqual(response.context["previous_day"], date(2026, 7, 3))
        self.assertIsNone(response.context["next_day"])
        earliest = self.day("2026-07-03")
        self.assertIsNone(earliest.context["previous_day"])
        self.assertEqual(earliest.context["next_day"], date(2026, 7, 8))
        empty = self.day("2026-07-05")
        self.assertEqual(list(empty.context["rows"]), [])
        self.assertContains(empty, 'value="2026-07-05"')
        self.assertEqual(empty.context["previous_day"], date(2026, 7, 3))
        self.assertEqual(empty.context["next_day"], date(2026, 7, 8))

    def test_navigation_includes_a_day_with_only_a_continuing_trip(self):
        start = datetime(2026, 7, 3, 23, 55, tzinfo=UTC)
        end = start + timedelta(days=1, minutes=10)
        for observed_at, address in (
            (start, "2 Market Street"),
            (end, "6 Harbour Road"),
        ):
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.case.owner,
                vehicle=self.vehicle,
                title="Vehicle position",
                address=address,
                observed_at=observed_at,
            )
        (generation,) = prepare_case_trips(self.case.owner, self.case.pk)
        # Exercise retained intervals independently of the current segmentation gap.
        with patch("geo.analysis.deterministic.trips.GAP_SECONDS", 2 * 24 * 60 * 60):
            for _ in range(2):
                process_trips(str(generation.pk), str(generation.token))
                generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        trip = saved_trips(self.case, state="all").get()

        self.login("viewer")
        self.assertEqual(self.day("2026-07-03").context["next_day"], date(2026, 7, 4))
        self.assertEqual(
            self.day("2026-07-05").context["previous_day"], date(2026, 7, 4)
        )
        middle = self.day("2026-07-04")
        self.assertEqual([row["id"] for row in middle.context["rows"]], [trip.pk])
        self.assertEqual(middle.context["previous_day"], date(2026, 7, 3))
        self.assertEqual(middle.context["next_day"], date(2026, 7, 5))

    def test_navigation_skips_a_date_omitted_by_the_local_timezone(self):
        viewer = self.users["viewer"]
        viewer.timezone = "Pacific/Apia"
        viewer.save(update_fields=["timezone"])
        midnight = datetime(2011, 12, 30, 10, tzinfo=UTC)
        self.text(collected_at=midnight - timedelta(minutes=5))
        self.text(collected_at=midnight)
        self.login("viewer")
        first = self.day("2011-12-29")
        self.assertEqual(first.context["next_day"], date(2011, 12, 31))
        omitted = self.day("2011-12-30")
        self.assertEqual(omitted.context["rows"], [])
        self.assertEqual(omitted.context["previous_day"], date(2011, 12, 29))
        self.assertEqual(omitted.context["next_day"], date(2011, 12, 31))

    def test_navigation_uses_rendered_day_when_local_clock_rolls_back_past_midnight(
        self,
    ):
        viewer = self.users["viewer"]
        viewer.timezone = "America/St_Johns"
        viewer.save(update_fields=["timezone"])
        midnight = datetime(2006, 10, 29, 2, 30, tzinfo=UTC)
        self.text(collected_at=midnight - timedelta(minutes=5))
        # This original displays a repeated 23:10 but follows the first midnight.
        latest = self.text(collected_at=midnight + timedelta(minutes=10))
        self.login("viewer")
        first = self.day("2006-10-28")
        self.assertEqual(first.context["next_day"], date(2006, 10, 29))
        last = self.day()
        self.assertEqual(last.context["day"], date(2006, 10, 29))
        self.assertEqual([row["id"] for row in last.context["rows"]], [latest.pk])
        self.assertEqual(last.context["previous_day"], date(2006, 10, 28))
        self.assertIsNone(last.context["next_day"])

    def test_archive_paging_and_stable_timestamp_ties(self):
        at = datetime(2026, 7, 3, 12, tzinfo=UTC)
        items = [self.text(collected_at=at) for _ in range(27)]
        ids = sorted(item.pk for item in items)
        TextEvidence.objects.filter(pk=ids[-1]).update(
            is_archived=True, archive_changed_at=at, archive_changed_by=self.case.owner
        )
        self.login("viewer")
        first = self.day("2026-07-03")
        second = self.day("2026-07-03", page=2)
        self.assertEqual([row["id"] for row in first.context["rows"]], ids[:25])
        self.assertEqual([row["id"] for row in second.context["rows"]], ids[25:])
        self.assertContains(second, "Archived")

    def test_deep_page_formats_only_visible_trip(self):
        start = datetime(2026, 7, 3, 8, tzinfo=UTC)
        common = {"case": self.case, "uploaded_by": self.case.owner}
        for hours in (0, 4):
            for minute, address in ((0, "2 Market Street"), (10, "6 Harbour Road")):
                VehicleLocationEvidence.objects.create(
                    **common,
                    vehicle=self.vehicle,
                    title="Vehicle position",
                    address=address,
                    observed_at=start + timedelta(hours=hours, minutes=minute),
                )
        (generation,) = prepare_case_trips(self.case.owner, self.case.pk)
        for _ in range(2):
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        earlier, later = saved_trips(self.case, state="all").order_by("start_at")
        body = "Synthetic collected text"
        digest = sha256(body.encode()).hexdigest()
        TextEvidence.objects.bulk_create(
            TextEvidence(
                **common,
                title=f"Collection {number}",
                body=body,
                sha256=digest,
                collected_at=start + timedelta(hours=1),
            )
            for number in range(500)
        )
        self.login("viewer")
        with patch(
            "geo.daily_chronology.format_trips", wraps=format_trips
        ) as summaries:
            response = self.day("2026-07-03", page=21)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["kind"] for row in response.context["rows"]], ["collected", "trip"]
        )
        summaries.assert_called_once_with(self.case, [later])
        self.assertNotIn(earlier.pk, [row["id"] for row in response.context["rows"]])

    def test_roles_invalid_date_and_inconsistent_links(self):
        at = datetime(2026, 6, 3, 8, tzinfo=UTC)
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Known office",
            address="Synthetic address",
            observed_at=at,
        )
        corrupted = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Invalid sighting",
            person=self.person,
            address="Synthetic address",
            observed_at=at,
            collected_at=at,
        )
        foreign = Person.objects.create(
            case=self.other_case, first_name="Foreign person"
        )
        PersonLocationEvidence.objects.filter(pk=corrupted.pk).update(person=foreign)
        for role in ("viewer", "editor", "owner"):
            with self.subTest(role=role):
                self.login(role)
                response = self.day("2026-06-03")
                self.assertEqual(
                    [row["id"] for row in response.context["rows"]], [location.pk]
                )
                self.assertNotContains(response, foreign.first_name)
        self.assertEqual(self.day("invalid").status_code, 400)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        for role in ("viewer", "outsider"):
            self.login(role)
            self.assertEqual(self.day("2026-06-03").status_code, 404)
