"""Authentication gate for investigator views, including enhanced uploads."""

from functools import wraps

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from accounts.verification import has_verified_email
from cases.permissions import get_case
from core.evidence.uploads import CASE_FILE_FORMS, admin_accepts_file, file_limit


def wants_json(request):
    return request.get_preferred_type(["text/html", "application/json"]) == (
        "application/json"
    )


def product_access_response(request):
    """Return the account gate without accessing the request body."""
    invitation = request.path.startswith("/invitations/")
    if not request.user.is_authenticated or not request.user.is_active:
        if invitation:
            request.session["pending_invitation_path"] = request.path
        if wants_json(request):
            return JsonResponse(
                {
                    "code": "authentication_required",
                    "message": "Sign in to continue.",
                },
                status=401,
            )
        if invitation:
            return redirect("account_login")
        return redirect_to_login(request.get_full_path())
    if not has_verified_email(request.user):
        if wants_json(request):
            return JsonResponse(
                {
                    "code": "verification_required",
                    "message": "Verify your email address to open case material.",
                },
                status=403,
            )
        if invitation:
            request.session["pending_invitation_path"] = request.path
        messages.info(request, "Verify your email address to open your cases.")
        return redirect("account_email")
    if request.session.get("pending_invitation_path") == request.path:
        request.session.pop("pending_invitation_path", None)
    return None


def product_login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = product_access_response(request)
        if response is not None:
            return response
        return view(request, *args, **kwargs)

    wrapped.product_login_required = True
    return wrapped


class ProductUploadAccessMiddleware(MiddlewareMixin):
    """Check upload permissions before receiving and parsing multipart bodies."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        multipart = (
            request.method == "POST" and request.content_type == "multipart/form-data"
        )
        if multipart and getattr(view_func, "product_login_required", False):
            if denied := product_access_response(request):
                return denied
        match = request.resolver_match
        allow_files = (
            multipart and match.app_name == "admin" and admin_accepts_file(request)
        )
        body = getattr(request, "scope", {}).get("pandect.body")
        if body is None:
            return None
        if multipart and match.view_name in CASE_FILE_FORMS:
            get_case(request.user, view_kwargs["case_id"], edit=True)
            allow_files = True

        # A request carries at most one file and a bounded amount of form data.
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if allow_files:
            limit += file_limit(match)
        length = request.META.get("CONTENT_LENGTH", "")
        if length and (
            not length.isascii() or not length.isdecimal() or len(length) > 20
        ):
            return HttpResponseBadRequest()
        if length and int(length) > limit:
            return self.too_large(request)
        try:
            received = async_to_sync(body.load)(limit, allow_files=allow_files)
        except ConnectionAbortedError:
            return HttpResponseBadRequest()
        if received is None:
            return self.too_large(request)
        if length and int(length) != received:
            return HttpResponseBadRequest()
        body.file.seek(0)
        # Django's multipart parser needs a length even with chunked transport.
        request.META["CONTENT_LENGTH"] = str(received)
        return None

    @staticmethod
    def too_large(request):
        message = "The request exceeds the configured upload limit."
        if wants_json(request):
            return JsonResponse(
                {"code": "file_too_large", "message": message}, status=413
            )
        return HttpResponse(message, status=413, content_type="text/plain")
