"""Authentication gate for investigator views, including enhanced uploads."""

from functools import wraps

from allauth.account.models import EmailAddress
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect


def wants_json(request):
    return "application/json" in request.headers.get("Accept", "")


def product_login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active:
            if request.path.startswith("/invitations/"):
                request.session["pending_invitation_path"] = request.path
            if wants_json(request):
                return JsonResponse(
                    {
                        "code": "authentication_required",
                        "message": "Sign in to continue.",
                    },
                    status=401,
                )
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
            if request.path.startswith("/invitations/"):
                request.session["pending_invitation_path"] = request.path
            messages.info(request, "Verify your email address to open your cases.")
            return redirect("account_email")
        if request.session.get("pending_invitation_path") == request.path:
            request.session.pop("pending_invitation_path", None)
        return view(request, *args, **kwargs)

    return wrapped
