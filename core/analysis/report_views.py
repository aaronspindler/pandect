"""Read current case reports and their filtered measurements."""

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.forms import report_form
from core.analysis.presentation import (
    measurement_formatter,
    prioritize_measurements,
)
from core.analysis.reports import REPORT_NAMES, report
from geo.analysis import presentation as geo_presentation
from geo.map_navigation import map_query
from phone.analysis import presentation as phone_presentation


@product_login_required
@require_safe
def current_report(request, case_id, name):
    case = get_case(request.user, case_id)
    if name == "vehicle-trips":
        return redirect(
            reverse("geo:case_map", args=(case.pk,), query=map_query(request.GET))
        )
    if name not in REPORT_NAMES:
        raise Http404("Report not found.")
    form = report_form(request.GET or None, case=case, report_name=name)
    filters_applied = any(
        request.GET.get(field)
        for field in (
            "start",
            "end",
            "vehicle_id",
            "participant_id",
            "other_participant_id",
            "duplicate_row",
        )
    )
    data = None
    if not form.is_bound or form.is_valid():
        values = form.cleaned_data if form.is_bound else {}
        try:
            data = report(
                case,
                name,
                page=request.GET.get("page", 1),
                pair_page=request.GET.get("pair_page", 1),
                duplicate_row=request.GET.get("duplicate_row") or None,
                **{k: v or None for k, v in values.items()},
            )
        except ValidationError as error:
            form.add_error(None, error)
    selected_names = []
    if data is not None:
        if form.is_bound:
            selected_names = [
                dict(form.fields[field].choices)[form.cleaned_data[field]]
                for field in ("vehicle_id", "participant_id", "other_participant_id")
                if form.cleaned_data.get(field)
            ]
        formatter = measurement_formatter(data, case=case, query=request.GET)
        data["measurements"] = formatter(data.get("metrics", {}))
        summary_fields = {
            "calls": (
                "Reported duration total (seconds, known only)",
                "Reported duration median (seconds, known only)",
                "Unknown duration",
                "Zero duration",
                "Duration distribution",
            ),
            "vehicle-coverage": (
                "Vehicles in selection",
                "Missing observation time in selection",
                "Intervals of at least 30 minutes",
                "Out-of-order CSV rows",
                "Negative receipt delays",
            ),
            "vehicle-speed": (
                "Known reported speeds",
                "Unknown reported speeds",
                "Zero reported speeds",
                "Reported speed median (km/h)",
                "Reported heading counts",
            ),
            "duplicates": ("Duplicate byte groups", "Occurrences in duplicate groups"),
        }
        prioritize_measurements(data, "Selected records", *summary_fields[name])
        for item in data["items"]:
            item["measurements"] = formatter(item.get("values", {}))
        if name == "calls":
            tables = phone_presentation.report_table(data)
        elif name in {"vehicle-coverage", "vehicle-speed"}:
            tables = geo_presentation.report_table(data, name)
        else:
            grouped = {}
            for item in data["items"]:
                grouped.setdefault(item["values"]["SHA-256"], []).append(item)
            tables = [
                {
                    "title": (
                        f"Group {items[0]['values']['Group']} · "
                        f"{items[0]['values']['Group occurrences']} identical files"
                    ),
                    "checksum": checksum,
                    "items": items,
                }
                for checksum, items in grouped.items()
            ]
    else:
        tables = []
    return render(
        request,
        "core/analysis/report.html",
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "form": form,
            "data": data,
            "name": name,
            "tables": tables,
            "filters_applied": filters_applied,
            "selected_names": selected_names,
            "duplicate_row": request.GET.get("duplicate_row", ""),
        },
    )
