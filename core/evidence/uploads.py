"""Bound each original while streaming, before it fills temporary storage."""

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.uploadhandler import FileUploadHandler, SkipFile, StopUpload

from cases.permissions import get_case

CASE_FILE_FORMS = {
    "core:evidence:create",
    "geo:vehicle_import_upload",
    "phone:add_attachment",
    "phone:imessage_upload",
}


def file_limit(match):
    if match.view_name == "geo:vehicle_import_upload":
        from geo.vehicle_imports import MAX_BYTES

        return min(settings.PANDECT_MAX_FILE_BYTES, MAX_BYTES)
    if match.view_name == "phone:imessage_upload":
        from phone.analysis.deterministic.imessage import import_file_limit

        return import_file_limit()
    return settings.PANDECT_MAX_FILE_BYTES


def admin_accepts_file(request):
    match = request.resolver_match
    model_admin = getattr(match.func, "model_admin", None)
    if not getattr(model_admin, "file_evidence", False):
        return False
    if getattr(match.func, "__wrapped__", None) != model_admin.add_view:
        return False
    # The admin add form selects its case inside the body. Its permission
    # requires at least one editable case and its case field checks the choice.
    if not (
        model_admin.admin_site.has_permission(request)
        and model_admin.has_add_permission(request)
    ):
        raise PermissionDenied
    return True


class LimitedUploadHandler(FileUploadHandler):
    def new_file(self, *args, **kwargs):
        # Public account forms also parse multipart POSTs during CSRF checks.
        # Refuse file parts there, and at any unauthorized upload entry point.
        match = self.request.resolver_match
        user = self.request.user
        if not user.is_authenticated or not user.is_active:
            raise PermissionDenied
        if match and match.app_name == "admin":
            if not user.is_staff:
                raise PermissionDenied
            if not admin_accepts_file(self.request):
                # Forged read-only originals must not replace retained files.
                # The other fields of a legitimate metadata edit still apply.
                raise SkipFile
        else:
            if (
                not match
                or match.view_name not in CASE_FILE_FORMS
                or not EmailAddress.objects.filter(user=user, verified=True).exists()
            ):
                raise PermissionDenied
            get_case(user, match.kwargs["case_id"], edit=True)
        super().new_file(*args, **kwargs)
        self.received = 0
        self.limit = file_limit(match)
        if self.content_length and self.content_length > self.limit:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=False)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.limit:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
