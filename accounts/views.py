from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .forms import TimezoneForm


@login_required
@require_http_methods(["GET", "POST"])
def timezone_settings(request):
    form = TimezoneForm(
        request.POST if request.method == "POST" else None, instance=request.user
    )
    if request.method == "POST" and form.is_valid():
        user = form.save(commit=False)
        user.save(update_fields=["timezone"])
        messages.success(request, "Timezone saved.")
        return redirect("account_timezone")
    return render(request, "accounts/timezone.html", {"form": form})
