"""Execute authorized analysis and atomically publish results and follow-ups."""

import hashlib
from itertools import batched

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from cases.models import Case
from cases.permissions import get_case, require_verified_email
from core.analysis.artifacts import (
    MAX_PREVIEW_TOTAL_BYTES,
    MAX_REFERENCE_TOTAL_BYTES,
    decode_artifact,
)
from core.analysis.diagnostics import log_analysis_failure, model_validation_code
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    requires_inference,
    validate_case_execution_grant,
    validate_execution,
)
from core.analysis.operations import OPERATIONS, get_operation
from core.analysis.published import PASSAGE_OPERATIONS
from core.analysis.result_validation import validate_output
from core.analysis.services import FINISHED
from core.analysis.source_resolution import resolve_processing_inputs
from core.analysis.transcripts import follow_up_transcription, transcript_group
from core.models import AnalysisArtifact, AnalysisResult, AnalysisRun, ExtractedPassage
from geo.analysis.external.geocoding import GeocodingError


def _authorize_run(run, *, lock=False):
    actor = get_user_model().objects.get(pk=run.requested_by_id)
    case = get_case(actor, run.case_id, edit=True, lock=lock)
    require_verified_email(actor, "Verify your email before processing.")
    if run.parameters.get("_search_generation"):
        raise AnalysisError("execution_changed")
    status = AnalysisRun.objects.values_list("status", flat=True).get(pk=run.pk)
    if status in FINISHED:
        raise AnalysisError("processing_cancelled")
    return case


def _authorize_execution(run, case):
    execution = validate_execution(run, case)
    if run.external_processing:
        validate_case_execution_grant(
            case,
            run.operation,
            execution,
            run.parameters.get("_external_approval", {}).get("revision"),
        )
    return execution


def authorize_run(run, *, lock=False, check_sources=True):
    case = _authorize_run(run, lock=lock)
    _authorize_execution(run, case)
    if check_sources:
        resolve_processing_inputs(run, case)
    return case


def _extracted_passages(result, sources):
    sources_by_id = {str(source.id): source for source in sources}
    for item_index, item in enumerate(result.data.get("items", [])):
        if not item.get("text"):
            continue
        for ref in item["source_ids"]:
            source = sources_by_id[ref]
            yield ExtractedPassage(
                case_id=result.case_id,
                result=result,
                kind=source.kind,
                evidence_id=source.item.pk,
                evidence_sha256=source.item.sha256,
                attachment_id=source.attachment.pk if source.attachment else None,
                text=item["text"],
                item_index=item_index,
                anchors=item.get("anchors", []),
            )


def process_run(run_id):
    with transaction.atomic():
        try:
            run = AnalysisRun.objects.select_for_update().get(pk=run_id)
        except AnalysisRun.DoesNotExist:
            return "unavailable"
        if run.status != "queued":
            return run.status
        run.status = "running"
        run.attempts += 1
        run.started_at = timezone.now()
        run.stage = AnalysisRun.Stage.SOURCES
        run.save(update_fields=["status", "attempts", "started_at", "stage"])
    stored_files = []
    import_rows = import_source_marker = None
    publication_finished = False
    stage = "authorization"
    try:
        case = _authorize_run(run)
        sources = resolve_processing_inputs(run, case)
        try:
            operation = get_operation(run.operation)
        except ValidationError as error:
            raise AnalysisError("processing_version_unavailable") from error
        if operation.version != run.method_version:
            raise AnalysisError("processing_version_unavailable")
        from core.analysis.gateway import GatewayClient

        execution = _authorize_execution(run, case)

        def authorize(**kw):
            return authorize_run(run, **kw)

        purpose = requires_inference(run.operation, run.parameters)
        client = GatewayClient(run, authorize)
        kwargs = {}
        specification = OPERATIONS[run.operation]
        if specification.inference in {"transcription", "vision"}:
            kwargs["client"] = client
        if specification.authorize_worker or specification.geographic:
            kwargs["authorize"] = authorize
        if purpose == "text":
            kwargs["infer"] = client.infer
        parameters = dict(run.parameters)
        if specification.geographic:
            parameters["external_processing"] = run.external_processing
        stage = "analysis"
        running = AnalysisRun.objects.filter(
            pk=run.pk, status=AnalysisRun.Status.RUNNING
        )
        if not running.update(stage=AnalysisRun.Stage.ANALYSIS):
            raise AnalysisError("processing_cancelled")
        if run.operation == "imessage_import" and parameters.get("phase") == "import":
            from phone.analysis.deterministic.imessage import stage_import

            data, import_rows, import_source_marker = stage_import(sources, parameters)
        else:
            data = operation.function(sources, parameters, **kwargs)
        stage = "validation"
        if not isinstance(data, dict):
            raise AnalysisError("invalid_result")
        if "_import_analysis" in parameters:
            from phone.analysis.ingest import describe_import_selection

            describe_import_selection(data, run)
        if "_reprocessing" in parameters:
            from core.analysis.reprocessing import describe_reprocessing_selection

            describe_reprocessing_selection(data, run)
        if "_comparison" in parameters:
            from core.analysis.history import describe_anchored_comparison

            describe_anchored_comparison(data, run)
        artifacts = data.pop("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 500:
            raise AnalysisError("invalid_preview")
        data = validate_output(data, sources, operation=run.operation)
        stage = "publication"
        if not running.update(stage=AnalysisRun.Stage.PUBLICATION):
            raise AnalysisError("processing_cancelled")
        with transaction.atomic():
            case = authorize_run(run, lock=True)
            current = AnalysisRun.objects.select_for_update().get(pk=run.pk)
            if current.status != "running":
                return current.status
            if (
                run.operation == "imessage_import"
                and run.parameters.get("phase") == "import"
            ):
                from phone.analysis.deterministic.imessage import apply_import

                apply_import(
                    run,
                    data,
                    sources,
                    publication=import_rows,
                    source_marker=import_source_marker,
                )
                # Publication adds exact message membership; it shares the result bound.
                data = validate_output(data, sources)
            result = AnalysisResult.objects.create(
                case=case,
                run=run,
                data=data,
                provenance={
                    "method_version": run.method_version,
                    "operation": run.operation,
                    "external_processing": run.external_processing,
                    "source_inputs": run.inputs,
                    **({"execution": execution} if execution is not None else {}),
                    **(
                        {"external_approval": run.parameters["_external_approval"]}
                        if run.external_processing
                        else {}
                    ),
                },
            )
            from core.analysis.catalogue import project_result

            project_result(result)
            if run.operation == "narrative_relationships":
                from core.relationships import publish_relationships

                publish_relationships(result, sources)
            totals = {kind: 0 for kind in AnalysisArtifact.Kind.values}
            reference_keys = set()
            prepared_findings = set()
            source_ids = {str(s.id) for s in sources}
            for payload in artifacts:
                raw, values = decode_artifact(payload, source_ids)
                kind = values["kind"]
                finding_index = values.get("metadata", {}).get("finding_index")
                if finding_index is not None:
                    if (
                        finding_index >= len(data.get("items", []))
                        or values["source_id"]
                        not in data["items"][finding_index]["source_ids"]
                        or finding_index in prepared_findings
                    ):
                        raise AnalysisError("invalid_artifact")
                    prepared_findings.add(finding_index)
                totals[kind] += len(raw)
                reference = kind == AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE
                limit = (
                    MAX_REFERENCE_TOTAL_BYTES if reference else MAX_PREVIEW_TOTAL_BYTES
                )
                if totals[kind] > limit:
                    raise AnalysisError("invalid_artifact")
                if reference:
                    key = values["metadata"]["reference_key"]
                    if key in reference_keys:
                        raise AnalysisError("invalid_artifact")
                    reference_keys.add(key)
                artifact = AnalysisArtifact(
                    case=case,
                    result=result,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    **values,
                )
                filename = "reference.json" if reference else "preview.jpg"
                artifact.file.save(filename, ContentFile(raw), save=False)
                stored_files.append((artifact.file.storage, artifact.file.name))
                artifact.save()
            if run.operation in PASSAGE_OPERATIONS:
                for batch in batched(
                    _extracted_passages(result, sources), 500, strict=False
                ):
                    ExtractedPassage.objects.bulk_create(batch)
            current.status = data.get("outcome", "complete")
            current.progress = 100
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "progress", "finished_at"])
            from core.case_search.indexing import result_published

            result_published(result)
            if not follow_up_transcription(run) and run.parameters.get(
                "automatic_followup"
            ):
                from core.analysis.automatic import follow_up_extraction

                follow_up_extraction(run, result)
            if (
                run.operation == "imessage_import"
                and run.parameters.get("phase") == "import"
            ):
                from phone.analysis.ingest import start_import_analysis

                start_import_analysis(run, data)
            if run.operation == "native_text":
                from core.analysis.extraction_reuse import follow_reused_extractions
                from core.evidence_chat.proposals import run_finished

                follow_reused_extractions(run, result)
                run_finished(current)
            publication_finished = True
            return str(result.pk)
    except (PermissionDenied, Http404) as error:
        code = "access_removed"
        log_analysis_failure(run, error, code, stage)
    except (AnalysisError, GeocodingError) as error:
        code = error.code
        log_analysis_failure(run, error, code, stage)
    except Exception as error:
        # Parser/provider strings can contain private source contents.
        code = model_validation_code(error) or "processing_failed"
        log_analysis_failure(run, error, code, stage)
    finally:
        if import_rows is not None:
            import_rows.close()
    from core.storage import delete_original

    # A failed COMMIT acknowledgement or on_commit callback can follow a durable
    # publication. Only a failure inside its body proves these files are unused.
    if not publication_finished:
        for storage, name in stored_files:
            delete_original(storage, name)
    with transaction.atomic():
        if transcript_group(run) or run.operation == "native_text":
            Case.objects.select_for_update().filter(pk=run.case_id).first()
        if AnalysisRun.objects.filter(pk=run_id, status="running").update(
            status="failed", error_code=code, finished_at=timezone.now()
        ):
            follow_up_transcription(run)
            if run.operation == "native_text":
                from core.evidence_chat.proposals import run_finished

                run.refresh_from_db(fields=["status", "error_code"])
                run_finished(run)
    return code
