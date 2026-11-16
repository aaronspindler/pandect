"""Authentication gate for investigator views, including enhanced uploads."""

from functools import wraps

from allauth.account.models import EmailAddress
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin


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
    if not EmailAddress.objects.filter(user=request.user, verified=True).exists():
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
    """Authorize multipart product requests before CSRF parses uploaded files."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        if (
            request.method != "POST"
            or request.content_type != "multipart/form-data"
            or not getattr(view_func, "product_login_required", False)
        ):
            return None
        return product_access_response(request)
