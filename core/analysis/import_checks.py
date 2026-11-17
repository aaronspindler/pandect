"""Deliberate, bounded local checks over server-owned completed import rows."""

from uuid import UUID, uuid5

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.services import resolve_inputs, start_run
from core.evidence.queries import scoped_evidence
from core.models import AnalysisRun, CaseAnalysisBatch
from geo.models import VehicleLocationImport


def imported_sources(case, scope):
    try:
        identifier = UUID(str(scope["id"]))
    except KeyError, ValueError, TypeError:
        raise Http404("Import not found.") from None
    if scope.get("type") == "vehicle":
        try:
            batch = VehicleLocationImport.objects.get(
                pk=identifier,
                case=case,
                document__case=case,
                status="complete",
                completed_at__isnull=False,
            )
        except VehicleLocationImport.DoesNotExist:
            raise Http404("Completed import not found.") from None
        return "vehicle-location", scoped_evidence(case, "vehicle-location").filter(
            import_batch=batch
        )
    if scope.get("type") == "messages":
        try:
            run = AnalysisRun.objects.get(
                pk=identifier,
                case=case,
                operation__in=("conversation_import", "imessage_import"),
                status__in=("complete", "partial"),
                result__case=case,
            )
        except AnalysisRun.DoesNotExist:
            raise Http404("Completed import not found.") from None
        if (
            run.operation == "imessage_import"
            and run.parameters.get("phase") != "import"
        ):
            raise Http404("Completed import not found.")
        sources = resolve_inputs(run, case, require_current_review=False)
        if len(sources) != 1:
            raise Http404("Import source unavailable.")
        if (
            run.operation == "imessage_import"
            and run.result.data.get("import_publication_version") == 2
        ):
            return "text-message", scoped_evidence(case, "text-message").filter(
                communication_context__case=case,
                communication_context__import_run=run,
                communication_context__source_document=sources[0].item,
                communication_context__source_sha256=sources[0].item.sha256,
            )
        if run.result.data.get("import_publication_version") != 1:
            raise AnalysisError("import_membership_unknown")
        published_rows = [
            item.get("values", {}).get("source_row")
            for item in run.result.data.get("items", [])
            if item.get("values", {}).get("import_disposition") == "created"
        ]
        published_ids = [
            item["values"]["message_id"]
            for item in run.result.data.get("items", [])
            if item.get("values", {}).get("import_disposition") == "created"
        ]
        return "text-message", scoped_evidence(case, "text-message").filter(
            pk__in=published_ids,
            communication_context__case=case,
            communication_context__source_document=sources[0].item,
            communication_context__source_sha256=sources[0].item.sha256,
            communication_context__source_row__in=published_rows,
        )
    raise Http404("Import not found.")


def prepare_import_checks(batch, case, rows):
    kind, queryset = imported_sources(case, batch.scope)
    sources = {
        item.pk: item for item in queryset.filter(pk__in=[row[0] for row in rows])
    }
    groups, current, characters = [], [], 0
    for pk, _ in rows:
        item = sources.get(pk)
        if item is None:
            batch.unavailable_sources += 1
            if len(batch.preparation_errors) < 100:
                batch.preparation_errors.append(
                    {
                        "kind": kind,
                        "evidence_id": str(pk),
                        "message": error_message("source_unavailable"),
                    }
                )
            continue
        length = len(getattr(item, "body", ""))
        if current and characters + length > 1_000_000:
            groups.append(current)
            current, characters = [], 0
        current.append({"kind": kind, "id": str(pk)})
        characters += length
    if current:
        groups.append(current)
    for group in groups:
        operations = (
            (("entities", {}), ("text_topics", {"include_summary": False}))
            if kind == "text-message"
            else (("location_quality", {}), ("normalize_addresses", {}))
        )
        for operation, parameters in operations:
            try:
                _, created = start_run(
                    batch.requested_by,
                    case.pk,
                    operation,
                    group,
                    parameters=parameters,
                    submission_id=uuid5(batch.pk, f"{operation}:{group[0]['id']}"),
                    batch=batch,
                )
            except (AnalysisError, ValidationError, Http404) as error:
                # No partially submitted group is described as complete.
                raise AnalysisError(
                    error.code
                    if isinstance(error, AnalysisError)
                    else "source_unavailable"
                ) from None
            batch.queued_runs += created


@product_login_required
@require_http_methods(["GET", "POST"])
def start(request, case_id, import_type, import_id):
    from core.analysis.bulk import start_case_analysis

    case = get_case(request.user, case_id, edit=True)
    scope = {"type": import_type, "id": str(import_id)}
    run = (
        AnalysisRun.objects.filter(
            case=case,
            pk=import_id,
            operation="imessage_import",
            method_version="2",
            parameters__phase="import",
        ).first()
        if import_type == "messages"
        else None
    )
    if run:
        from phone.analysis.ingest import import_analysis_scope

        scope = import_analysis_scope(run)
    elif request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        imported_sources(case, scope)
    except AnalysisError as error:
        messages.error(request, error_message(error.code))
        return redirect("core:analysis:processing", case_id=case.pk)
    if run:
        from phone.imessage_forms import IMessageAnalysisRetryForm

        latest = CaseAnalysisBatch.objects.filter(
            case=case,
            scope__type="messages",
            scope__id=str(run.pk),
            scope__automatic=True,
        ).first()
        form = IMessageAnalysisRetryForm(
            request.POST if request.method == "POST" else None,
            actor=request.user,
            case=case,
            run=run,
            initial={"mode": (latest.scope if latest else scope).get("mode", "local")},
        )
    else:
        form = EvidenceAnalysisForm(request.POST)
    if request.method == "POST" and form.is_valid():
        try:
            if run:
                from phone.imessage_imports import validate_import_analysis

                scope = {
                    "type": "messages",
                    "id": str(run.pk),
                    "automatic": True,
                } | validate_import_analysis(form.specification(), actor=request.user)
            batch, created = start_case_analysis(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                scope=scope,
            )
        except ValidationError, DatabaseError, AnalysisError:
            form.add_error(
                None, "Analysis could not be queued. Review the settings and try again."
            )
        else:
            messages.success(
                request,
                "Message analysis requested."
                if created and scope.get("automatic")
                else "Local checks requested."
                if created
                else "This analysis was already requested.",
            )
            return redirect(
                reverse("core:analysis:processing", args=[case.pk])
                + f"?batch={batch.pk}"
            )
    if run:
        return render(
            request,
            "phone/imessage_analysis.html",
            case_context(case, request.user)
            | {"nav_section": "evidence", "form": form, "run": run},
            status=400 if form.errors else 200,
        )
    messages.error(request, "Return to the import and submit its analysis again.")
    return redirect("core:analysis:processing", case_id=case.pk)
