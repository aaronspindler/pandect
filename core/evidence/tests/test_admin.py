import hashlib
import tempfile
from uuid import uuid4

from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cases.models import Case, CaseMembership
from core.evidence.form_registry import ORIGINAL_FIELDS
from core.evidence.types import COMMUNICATION_KINDS, FILE_KINDS, KINDS, LOCATION_KINDS
from geo.models import VehicleLocationImport
from people.models import Person
from phone.models import CallEvidenceAttachment, PhoneCallEvidence
from vehicle.models import Vehicle


class EvidenceAdminTests(TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="pandect-evidence-admin-")
        cls.addClassCleanup(directory.cleanup)
        cls.enterClassContext(override_settings(MEDIA_ROOT=directory.name))
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.owner = cls.staff("owner")
        cls.editor = cls.staff("editor")
        cls.viewer = cls.staff("viewer")
        cls.outsider = cls.staff("outsider")
        cls.superuser = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=None
        )
        cls.case = Case.objects.create(owner=cls.owner, name="Shared investigation")
        cls.other_case = Case.objects.create(owner=cls.outsider, name="Hidden case")
        cls.membership = CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)
        cls.person = Person.objects.create(case=cls.case, first_name="Alex Example")
        cls.vehicle = Vehicle.objects.create(case=cls.case, model="Synthetic van")
        cls.other_vehicle = Vehicle.objects.create(
            case=cls.other_case, model="Hidden van"
        )
        cls.other_person = Person.objects.create(
            case=cls.other_case, first_name="Hidden person"
        )
        cls.items = {}
        cls.other_items = {}
        for kind, model in KINDS.items():
            cls.items[kind] = model.objects.create(
                case=cls.case, uploaded_by=cls.owner, **cls.original(kind), title=kind
            )
            cls.other_items[kind] = model.objects.create(
                case=cls.other_case,
                uploaded_by=cls.outsider,
                **cls.original(
                    kind, person=cls.other_person, vehicle=cls.other_vehicle
                ),
                title="Hidden original",
            )

    @staticmethod
    def staff(username):
        user = get_user_model().objects.create_user(username=username, is_staff=True)
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=(
                    "text",
                    "image",
                    "audio",
                    "document",
                    "geo",
                    "phone",
                    "cases",
                    "people",
                    "vehicle",
                )
            )
        )
        return user

    @classmethod
    def original(cls, kind, *, person=None, vehicle=None):
        if kind in FILE_KINDS:
            return {"file": SimpleUploadedFile("original.txt", b"Synthetic original.")}
        if kind in COMMUNICATION_KINDS:
            return {
                "sender": person or cls.person,
                "recipient": person or cls.person,
                **(
                    {"body": "  Original message.\r\n"}
                    if kind == "text-message"
                    else {}
                ),
            }
        if kind == "text":
            return {"body": "  Synthetic original.\r\n "}
        return {
            "address": "  Synthetic harbour office\n ",
            **({"person": person or cls.person} if kind == "person-location" else {}),
            **(
                {"vehicle": vehicle or cls.vehicle}
                if kind == "vehicle-location"
                else {}
            ),
        }

    @staticmethod
    def url(kind, action="changelist", item=None):
        return reverse(
            f"admin:{KINDS[kind]._meta.app_label}_{KINDS[kind]._meta.model_name}_{action}",
            args=[item.pk] if item else None,
        )

    def data(self, **overrides):
        if isinstance(overrides.get("person"), Person):
            overrides["person"] = overrides["person"].pk
        for field in ("sender", "recipient"):
            if isinstance(overrides.get(field), Person):
                overrides[field] = overrides[field].pk
        if isinstance(overrides.get("vehicle"), Vehicle):
            overrides["vehicle"] = overrides["vehicle"].pk
        return {
            "case": self.case.pk,
            "title": "New source",
            "_save": "Save",
            **overrides,
        }

    def test_all_concrete_evidence_models_are_registered(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("admin:index"))
        for kind, model in KINDS.items():
            with self.subTest(kind=kind):
                self.assertTrue(admin.site.is_registered(model))
                self.assertContains(response, self.url(kind))

    def test_active_staff_and_model_permissions_are_required(self):
        for user in (None, self.owner):
            if user:
                user.is_staff = False
                user.save()
                self.client.force_login(user)
            for kind in KINDS:
                self.assertEqual(self.client.get(self.url(kind)).status_code, 302)
        self.owner.is_staff = True
        self.owner.is_active = False
        self.owner.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url("text")).status_code, 302)
        self.owner.is_active = True
        self.owner.save()
        self.owner.user_permissions.clear()
        self.client.force_login(self.owner)
        for kind, item in self.items.items():
            for action in ("changelist", "add", "change", "history", "delete"):
                with self.subTest(kind=kind, action=action):
                    target = item if action in ("change", "history", "delete") else None
                    self.assertEqual(
                        self.client.get(self.url(kind, action, target)).status_code, 403
                    )

    def test_lists_searches_and_archive_filters_remain_case_scoped(self):
        for user, own in (
            (self.owner, True),
            (self.editor, True),
            (self.viewer, True),
            (self.outsider, False),
            (self.superuser, None),
        ):
            self.client.force_login(user)
            for kind in KINDS:
                with self.subTest(user=user, kind=kind):
                    response = self.client.get(self.url(kind))
                    expected = (
                        []
                        if own is None
                        else [self.items[kind] if own else self.other_items[kind]]
                    )
                    self.assertCountEqual(response.context["cl"].queryset, expected)
                    response = self.client.get(self.url(kind), {"q": "Hidden"})
                    self.assertCountEqual(
                        response.context["cl"].queryset,
                        [self.other_items[kind]] if own is False else [],
                    )
                    response = self.client.get(
                        self.url(kind), {"is_archived__exact": "0"}
                    )
                    self.assertCountEqual(response.context["cl"].queryset, expected)

    def test_owner_and_editor_can_create_all_types_with_server_attribution(self):
        for user in (self.owner, self.editor):
            self.client.force_login(user)
            for kind, model in KINDS.items():
                with self.subTest(user=user, kind=kind):
                    response = self.client.post(
                        self.url(kind, "add"),
                        self.data(
                            **self.original(kind),
                            uploaded_by=self.outsider.pk,
                            sha256="0" * 64,
                            submission_id=uuid4(),
                            is_archived="on",
                        ),
                    )
                    self.assertRedirects(response, self.url(kind))
                    item = model.objects.latest("created_at")
                    self.assertEqual(item.uploaded_by, user)
                    self.assertEqual(item.case, self.case)
                    self.assertNotEqual(item.sha256, "0" * 64)
                    self.assertIsNone(item.submission_id)
                    self.assertFalse(item.is_archived)
                    if kind == "text":
                        self.assertEqual(item.body, self.original(kind)["body"])
                        self.assertEqual(
                            item.sha256, hashlib.sha256(item.body.encode()).hexdigest()
                        )
                    elif kind in FILE_KINDS:
                        with item.file.open("rb") as original:
                            self.assertEqual(original.read(), b"Synthetic original.")
                    elif kind in LOCATION_KINDS:
                        self.assertEqual(item.address, self.original(kind)["address"])
                    else:
                        self.assertEqual(item.sender, self.person)
                    self.assertTrue(
                        LogEntry.objects.filter(
                            object_id=str(item.pk), user=user, action_flag=ADDITION
                        ).exists()
                    )

    def test_creation_choices_and_forged_case_are_restricted(self):
        self.client.force_login(self.editor)
        for kind, model in KINDS.items():
            with self.subTest(kind=kind):
                response = self.client.get(self.url(kind, "add"))
                self.assertCountEqual(
                    response.context["adminform"].form.fields["case"].queryset,
                    [self.case],
                )
                response = self.client.post(
                    self.url(kind, "add"),
                    self.data(**self.original(kind), case=self.other_case.pk),
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("case", response.context["adminform"].form.errors)
                self.assertEqual(model.objects.count(), 2)

    def test_person_choices_and_validation_prevent_cross_case_links(self):
        second_case = Case.objects.create(owner=self.owner)
        second_person = Person.objects.create(
            case=second_case, first_name="Other case person"
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.url("person-location", "add"))
        self.assertCountEqual(
            response.context["adminform"].form.fields["person"].queryset,
            [self.person, second_person],
        )
        for person in (self.other_person, second_person):
            response = self.client.post(
                self.url("person-location", "add"),
                self.data(address="Synthetic office", person=person.pk),
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("person", response.context["adminform"].form.errors)
        self.assertEqual(KINDS["person-location"].objects.count(), 2)

    def test_viewers_and_unrelated_superuser_cannot_add_or_edit(self):
        CaseMembership.objects.create(case=self.case, user=self.superuser)
        for user in (self.viewer, self.superuser):
            self.client.force_login(user)
            for kind, item in self.items.items():
                with self.subTest(user=user, kind=kind):
                    response = self.client.get(self.url(kind, "change", item))
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.context["has_change_permission"])
                    self.assertNotContains(response, 'name="title"')
                    for action, target in (("add", None), ("change", item)):
                        self.assertEqual(
                            self.client.post(
                                self.url(kind, action, target),
                                self.data(**self.original(kind)),
                            ).status_code,
                            403,
                        )

    def test_inconsistent_person_link_cannot_disclose_another_case(self):
        item = self.items["person-location"]
        # Trusted bulk writes bypass model validation; admin still fails closed.
        type(item).objects.filter(pk=item.pk).update(person=self.other_person)
        self.client.force_login(self.owner)
        response = self.client.get(self.url("person-location"))
        self.assertEqual(list(response.context["cl"].queryset), [])
        self.assertRedirects(
            self.client.get(self.url("person-location", "change", item)),
            reverse("admin:index"),
        )

    def test_inconsistent_vehicle_import_sources_are_hidden_from_admin(self):
        item = self.items["vehicle-location"]
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            document=self.items["document"],
            timezone="UTC",
        )
        type(item).objects.filter(pk=item.pk).update(import_batch=batch, source_row=2)
        self.client.force_login(self.owner)
        response = self.client.get(self.url("vehicle-location"))
        self.assertEqual(list(response.context["cl"].queryset), [item])
        for changes in (
            {"case": self.other_case},
            {"document": self.other_items["document"]},
        ):
            # Trusted bulk writes can create an inconsistent provenance chain.
            VehicleLocationImport.objects.filter(pk=batch.pk).update(**changes)
            for user in (self.owner, self.editor, self.viewer):
                with self.subTest(changes=changes, user=user):
                    self.client.force_login(user)
                    response = self.client.get(self.url("vehicle-location"))
                    self.assertEqual(list(response.context["cl"].queryset), [])
                    for action in ("change", "history"):
                        self.assertRedirects(
                            self.client.get(self.url("vehicle-location", action, item)),
                            reverse("admin:index"),
                        )
            VehicleLocationImport.objects.filter(pk=batch.pk).update(
                case=self.case, document=self.items["document"]
            )

    def test_edits_ignore_forged_originals_and_record_history(self):
        self.client.force_login(self.editor)
        for kind, item in self.items.items():
            with self.subTest(kind=kind):
                expected = {name: getattr(item, name) for name in item.immutable_fields}
                item.description = "Earlier investigator context"
                item.save(update_fields=["description"])
                response = self.client.get(self.url(kind, "change", item))
                self.assertContains(response, "Earlier investigator context")
                for name in (
                    "case",
                    "uploaded_by",
                    "sha256",
                    "description",
                    *ORIGINAL_FIELDS[kind],
                ):
                    self.assertNotContains(response, f'name="{name}"')
                if kind in FILE_KINDS:
                    self.assertNotContains(response, item.file.name)
                    self.assertNotContains(response, item.file.url)
                response = self.client.post(
                    self.url(kind, "change", item),
                    self.data(
                        **{
                            **self.original(kind, person=self.other_person),
                            "body": "Changed original",
                            "address": "Changed original",
                        },
                        case=self.other_case.pk,
                        uploaded_by=self.outsider.pk,
                        sha256="0" * 64,
                        declared_content_type="text/html",
                        description="Analyst correction",
                        source="Synthetic witness",
                    ),
                )
                self.assertRedirects(response, self.url(kind))
                item.refresh_from_db()
                self.assertEqual(item.description, "Earlier investigator context")
                self.assertEqual(item.source, "Synthetic witness")
                for name, value in expected.items():
                    self.assertEqual(getattr(item, name), value)
                self.assertTrue(
                    LogEntry.objects.filter(
                        object_id=str(item.pk), user=self.editor, action_flag=CHANGE
                    ).exists()
                )

    def test_archive_restore_attribution_and_noop(self):
        self.client.force_login(self.editor)
        for kind, item in self.items.items():
            with self.subTest(kind=kind):
                for archived in (True, True, False):
                    previous = item.archive_changed_at
                    changed = item.is_archived != archived
                    response = self.client.post(
                        self.url(kind, "change", item),
                        self.data(
                            **({"is_archived": "on"} if archived else {}),
                            archive_changed_by=self.outsider.pk,
                            archive_changed_at="2000-01-01",
                        ),
                    )
                    self.assertRedirects(response, self.url(kind))
                    item.refresh_from_db()
                    self.assertEqual(item.is_archived, archived)
                    self.assertEqual(item.archive_changed_by, self.editor)
                    if changed:
                        self.assertNotEqual(item.archive_changed_at, previous)
                    else:
                        self.assertEqual(item.archive_changed_at, previous)

    def test_unrelated_users_cannot_access_direct_object_or_history_urls(self):
        for user in (self.outsider, self.superuser):
            self.client.force_login(user)
            for kind, item in self.items.items():
                for action in ("change", "history"):
                    with self.subTest(user=user, kind=kind, action=action):
                        for method in (self.client.get, self.client.post):
                            self.assertRedirects(
                                method(self.url(kind, action, item), self.data()),
                                reverse("admin:index"),
                            )

    def test_revocation_and_downgrade_affect_existing_sessions(self):
        self.client.force_login(self.editor)
        self.membership.role = CaseMembership.Role.VIEWER
        self.membership.save()
        for kind, item in self.items.items():
            self.assertEqual(
                self.client.post(
                    self.url(kind, "change", item), self.data()
                ).status_code,
                403,
            )
            self.assertEqual(self.client.get(self.url(kind, "add")).status_code, 403)
        self.membership.delete()
        for kind, item in self.items.items():
            self.assertEqual(
                list(self.client.get(self.url(kind)).context["cl"].queryset), []
            )
            for action in ("change", "history"):
                self.assertRedirects(
                    self.client.get(self.url(kind, action, item)),
                    reverse("admin:index"),
                )

    def test_model_permissions_gate_add_and_change(self):
        for kind, item in self.items.items():
            for action, codename, target in (
                ("add", "add", None),
                ("change", "change", item),
            ):
                with self.subTest(kind=kind, action=action):
                    self.owner.user_permissions.set(
                        Permission.objects.filter(
                            content_type__app_label__in=(
                                "text",
                                "image",
                                "audio",
                                "document",
                                "geo",
                                "phone",
                            )
                        ).exclude(codename=f"{codename}_{item._meta.model_name}")
                    )
                    self.client.force_login(self.owner)
                    self.assertEqual(
                        self.client.post(
                            self.url(kind, action, target),
                            self.data(**self.original(kind)),
                        ).status_code,
                        403,
                    )

    def test_individual_and_bulk_deletion_are_disabled_even_for_owners(self):
        self.client.force_login(self.owner)
        for kind, item in self.items.items():
            for method in (self.client.get, self.client.post):
                self.assertEqual(
                    method(self.url(kind, "delete", item), {"post": "yes"}).status_code,
                    403,
                )
            response = self.client.post(
                self.url(kind),
                {
                    "action": "delete_selected",
                    "_selected_action": item.pk,
                    "post": "yes",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    @override_settings(PANDECT_MAX_FILE_BYTES=8, PANDECT_MAX_TEXT_BYTES=8)
    def test_admin_enforces_upload_and_text_limits(self):
        self.client.force_login(self.owner)
        for kind in (*FILE_KINDS, "text"):
            response = self.client.post(
                self.url(kind, "add"), self.data(**self.original(kind))
            )
            self.assertEqual(response.status_code, 200)
            field = "file" if kind in FILE_KINDS else "body"
            self.assertIn(field, response.context["adminform"].form.errors)
            self.assertEqual(KINDS[kind].objects.count(), 2)

    def test_csrf_is_required_for_writes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for kind, item in self.items.items():
            self.assertEqual(
                client.post(self.url(kind, "add"), self.data()).status_code, 403
            )
            self.assertEqual(
                client.post(self.url(kind, "change", item), self.data()).status_code,
                403,
            )

    def test_case_deletion_requires_evidence_permissions_and_cleans_up_files(self):
        permission = Permission.objects.get(
            content_type__app_label="text", codename="delete_textevidence"
        )
        self.owner.user_permissions.remove(permission)
        self.client.force_login(self.owner)
        url = reverse("admin:cases_case_delete", args=[self.case.pk])
        response = self.client.get(url)
        self.assertIn("text evidence", response.context["perms_lacking"])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.owner.user_permissions.add(permission)
        self.client.force_login(self.owner)
        files = [self.items[kind].file for kind in FILE_KINDS]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"post": "yes"})
        self.assertRedirects(response, reverse("admin:cases_case_changelist"))
        self.assertFalse(Case.objects.filter(pk=self.case.pk).exists())
        for original in files:
            self.assertFalse(original.storage.exists(original.name))
        for model in KINDS.values():
            self.assertEqual(model.objects.count(), 1)
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.other_person.pk).exists())

    def test_retained_call_attachments_are_readonly_without_storage_urls(self):
        item = self.items["phone-call"]
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            phone_call=item,
            transcript="Supplied transcript",
            file=SimpleUploadedFile("call.wav", b"audio"),
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.url("phone-call", "change", item))
        self.assertContains(response, "Supplied transcript")
        self.assertContains(response, "Inspect or add retained material")
        self.assertNotContains(response, addition.file.name)
        self.assertNotContains(response, addition.file.url)
        inline = admin.site._registry[PhoneCallEvidence].get_inline_instances(
            response.wsgi_request, item
        )[0]
        self.assertFalse(inline.has_add_permission(response.wsgi_request, item))
        self.assertFalse(inline.has_change_permission(response.wsgi_request, item))
        self.assertFalse(inline.has_delete_permission(response.wsgi_request, item))
