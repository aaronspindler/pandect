"""Per-request timezones preserve instants, originals and form interpretation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import TimezoneMiddleware
from core.analysis.forms import analysis_form, report_form
from core.evidence.form_registry import evidence_form
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence, VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips
from phone.models import PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


class TimezoneMiddlewareTests(SimpleTestCase):
    def test_preference_cookie_and_invalid_timezone_are_scoped_to_request(self):
        for preference, cookie, expected in (
            ("America/Toronto", "Asia/Tokyo", "America/Toronto"),
            ("", "Asia/Kolkata", "Asia/Kolkata"),
            ("", "", "UTC"),
            ("", "Unknown/Timezone", "UTC"),
            ("", "../UTC", "UTC"),
            ("", "a" * 101, "UTC"),
        ):
            with self.subTest(preference=preference, cookie=cookie):
                request = RequestFactory().get("/")
                request.user = SimpleNamespace(
                    is_authenticated=True, timezone=preference
                )
                request.COOKIES["pandect_timezone"] = cookie

                def respond(request, expected=expected):
                    self.assertEqual(timezone.get_current_timezone_name(), expected)
                    return HttpResponse()

                with timezone.override("Europe/Paris"):
                    TimezoneMiddleware(respond)(request)
                    self.assertEqual(
                        timezone.get_current_timezone_name(), "Europe/Paris"
                    )

    def test_timezone_context_is_restored_after_an_exception(self):
        request = RequestFactory().get("/")
        request.user = SimpleNamespace(is_authenticated=False)
        request.COOKIES["pandect_timezone"] = "America/Toronto"

        def fail(request):
            self.assertEqual(timezone.get_current_timezone_name(), "America/Toronto")
            raise RuntimeError("Synthetic rendering failure")

        with timezone.override("Asia/Tokyo"):
            with self.assertRaises(RuntimeError):
                TimezoneMiddleware(fail)(request)
            self.assertEqual(timezone.get_current_timezone_name(), "Asia/Tokyo")


class UserTimezoneTests(EvidenceViewTestMixin, TestCase):
    def test_setting_requires_authentication_and_csrf(self):
        url = reverse("account_timezone")
        self.assertRedirects(
            self.client.get(url), f"{reverse('account_login')}?next={url}"
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users["viewer"])
        self.assertEqual(client.post(url, {"timezone": "Asia/Tokyo"}).status_code, 403)

    def test_setting_updates_only_current_account_and_survives_a_new_session(self):
        self.login("viewer")
        url = reverse("account_timezone")
        self.client.cookies["pandect_timezone"] = "Asia/Tokyo"
        response = self.client.post(url, {"timezone": "America/Toronto"}, follow=True)
        self.assertContains(
            response, "Times are currently shown in <strong>America/Toronto"
        )
        self.assertEqual(
            get_user_model().objects.get(pk=self.users["viewer"].pk).timezone,
            "America/Toronto",
        )
        self.assertEqual(
            get_user_model().objects.get(pk=self.users["owner"].pk).timezone, ""
        )
        client = Client()
        client.force_login(self.users["viewer"])
        self.assertContains(client.get(url), "<strong>America/Toronto</strong>")
        for invalid in ("Unknown/Timezone", "../UTC"):
            response = self.client.post(url, {"timezone": invalid})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertContains(self.client.get(url), "<strong>America/Toronto</strong>")
        self.client.post(url, {"timezone": ""})
        self.assertContains(self.client.get(url), "<strong>Asia/Tokyo</strong>")

    def test_evidence_times_follow_each_viewer_with_dst_and_date_rollover(self):
        evidence = self.text(collected_at=datetime(2026, 7, 1, 0, 30, tzinfo=UTC))
        original_hash = evidence.sha256
        for zone, expected in (
            ("America/Toronto", "30 Jun 2026, 20:30 EDT"),
            ("Asia/Kolkata", "01 Jul 2026, 06:00 IST"),
            ("UTC", "01 Jul 2026, 00:30 UTC"),
        ):
            with self.subTest(zone=zone):
                self.login("viewer")
                self.client.cookies["pandect_timezone"] = zone
                self.assertContains(
                    self.client.get(self.url("detail", evidence)), expected
                )
        evidence.refresh_from_db()
        self.assertEqual(evidence.collected_at, datetime(2026, 7, 1, 0, 30, tzinfo=UTC))
        self.assertEqual(evidence.sha256, original_hash)
        evidence.collected_at = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
        evidence.save()
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        self.assertContains(
            self.client.get(self.url("detail", evidence)), "31 Dec 2025, 19:30 EST"
        )

    def test_local_datetime_inputs_save_the_correct_instant_and_reject_dst_gaps(self):
        self.login("editor")
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        response = self.client.get(self.url("create"), {"kind": "text"})
        self.assertContains(response, "Source collected at (America/Toronto)")
        response = self.client.post(
            self.url("create"),
            self.payload(collected_at="2026-07-01T00:30", submission_id=uuid4()),
        )
        self.assertEqual(response.status_code, 302)
        evidence = TextEvidence.objects.get(case=self.case)
        self.assertEqual(evidence.collected_at, datetime(2026, 7, 1, 4, 30, tzinfo=UTC))
        form_class = evidence_form("text")
        with timezone.override("America/Toronto"):
            for value in ("2026-03-08T02:30", "2026-11-01T01:30"):
                with self.subTest(value=value):
                    form = form_class(self.payload(collected_at=value), kind="text")
                    self.assertFalse(form.is_valid())
                    self.assertIn("collected_at", form.errors)
            edit = evidence_form("text", editing=True)(kind="text", instance=evidence)
            self.assertIn('value="2026-07-01T00:30"', str(edit["collected_at"]))

    def test_domain_and_report_fields_use_their_labeled_timezones(self):
        with timezone.override("America/Toronto"):
            for kind, model, field, label in (
                ("location", LocationEvidence, "observed_at", "Observed at"),
                ("text-message", TextMessageEvidence, "occurred_at", "Sent at"),
            ):
                form = evidence_form(kind)(kind=kind, instance=model(case=self.case))
                self.assertEqual(form.fields[field].label, f"{label} (America/Toronto)")
            form = report_form(
                {"state": "active", "start": "2026-07-01T00:30"},
                case=self.case,
                report_name="calls",
            )
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.fields["start"].label, "From (America/Toronto)")
            self.assertEqual(
                form.cleaned_data["start"], datetime(2026, 7, 1, 4, 30, tzinfo=UTC)
            )
            form = analysis_form(case=self.case, operation="geographic_context")
            self.assertEqual(
                form.fields["context_as_of"].label,
                "Historical map snapshot (UTC, optional)",
            )
            form = analysis_form(case=self.case, operation="comm_patterns")
            self.assertEqual(
                form.fields["compare_split_at"].label,
                "Compare communications before and after (America/Toronto)",
            )

    def test_trip_arrival_date_uses_local_midnight(self):
        self.login("viewer")
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        for time, address in (
            (datetime(2026, 7, 1, 3, 55, tzinfo=UTC), "10 Example Street"),
            (datetime(2026, 7, 1, 4, 5, tzinfo=UTC), "20 Example Street"),
        ):
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                vehicle=self.vehicle,
                title="Synthetic observation",
                address=address,
                observed_at=time,
            )
        generation = prepare_case_trips(self.users["owner"], self.case.pk)[0]
        for _ in range(10):
            if generation.status != "queued":
                break
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        response = self.client.get(
            reverse("core:analysis:report", args=(self.case.pk, "vehicle-trips"))
        )
        self.assertContains(response, ">30 Jun 2026</time>")
        self.assertContains(response, ">23:55 EDT</time>")
        self.assertContains(response, ">1 Jul 2026, 00:05 EDT</time>")

    def test_call_report_localizes_rows_and_filters_without_changing_utc_statistics(
        self,
    ):
        PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic call",
            sender=self.person,
            recipient=self.person,
            occurred_at=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
        )
        self.login("viewer")
        self.client.cookies["pandect_timezone"] = "America/Toronto"
        url = reverse("core:analysis:report", args=(self.case.pk, "calls"))
        response = self.client.get(
            url,
            {"state": "active", "start": "2026-06-30T20:00", "end": "2026-06-30T21:00"},
        )
        self.assertContains(response, "30 Jun 2026, 20:30:00 EDT")
        self.assertContains(response, 'datetime="2026-07-01T00:30:00+00:00"')
        self.assertNotContains(response, "Recorded call time (UTC)")
        self.assertContains(response, "Recorded UTC hour counts")
        self.assertContains(response, "Recorded hour counts use UTC.")
