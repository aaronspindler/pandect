"""Register canonical source changes transactionally, including whole-case deletion."""

from itertools import batched

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from core.evidence.types import KINDS
from core.models import ExtractedPassage, HostedSearchIndex
from phone.models import CallEvidenceAttachment

from .lifecycle import changed, reconcile, retire

RECORDS = {
    "people.Person": "person",
    "vehicle.Vehicle": "vehicle",
    "geo.PointOfInterest": "poi",
}


@receiver(pre_save, sender="people.Person")
@receiver(pre_save, sender="vehicle.Vehicle")
def remember_label(sender, instance, raw=False, using=None, **kwargs):
    if raw or instance._state.adding:
        return
    previous = sender.objects.using(using).filter(pk=instance.pk).first()
    instance._previous_search_label = str(previous) if previous else None


@receiver(pre_delete, sender="cases.Case")
def deleting_case(sender, instance, **kwargs):
    for index in HostedSearchIndex.objects.select_for_update().filter(
        case_key=instance.pk, retired=False
    ):
        retire(index)


def source_changed(
    sender, instance, raw=False, created=None, update_fields=None, **kwargs
):
    if raw:
        return
    label = sender._meta.label
    if label == "socialmedia.SocialAccount":
        changed(instance.person.case_id, "social", "person", [instance.pk])
    elif label in RECORDS:
        case_id = (
            instance.person.case_id
            if label == "geo.PointOfInterest"
            else instance.case_id
        )
        changed(case_id, "record", RECORDS[label], [instance.pk])
        if (
            created is False
            and label in ("people.Person", "vehicle.Vehicle")
            and getattr(instance, "_previous_search_label", None) != str(instance)
        ):
            # Evidence metadata, POIs and reported accounts include these entity labels.
            reconcile(case_id)
    elif label == "phone.CallEvidenceAttachment":
        kind = "phone-call" if instance.phone_call_id else "facetime"
        changed(instance.case_id, "attachment", kind, [instance.pk])
    else:
        kind = next(k for k, model in KINDS.items() if model is sender)
        active = changed(instance.case_id, "original", kind, [instance.pk])
        dates = {"collected_at", "observed_at", "occurred_at"}
        if (
            active
            and created is False
            and (update_fields is None or dates.intersection(update_fields))
        ):
            # Retained passages and call additions use the original's event date.
            passages = ExtractedPassage.objects.filter(
                case_id=instance.case_id, kind=kind, evidence_id=instance.pk
            )
            refresh_dependencies(instance.case_id, "passage", "", passages)
            if kind in ("phone-call", "facetime"):
                relation = "phone_call_id" if kind == "phone-call" else "facetime_id"
                attachments = CallEvidenceAttachment.objects.filter(
                    case_id=instance.case_id, **{relation: instance.pk}
                )
                refresh_dependencies(instance.case_id, "attachment", kind, attachments)


def refresh_dependencies(case_id, surface, kind, queryset):
    keys = queryset.values_list("pk", flat=True).iterator(chunk_size=500)
    for ids in batched(keys, 500, strict=False):
        changed(case_id, surface, kind, ids)


for model in (
    *KINDS.values(),
    *RECORDS,
    "phone.CallEvidenceAttachment",
    "socialmedia.SocialAccount",
):
    post_save.connect(source_changed, sender=model)
    post_delete.connect(source_changed, sender=model)


@receiver(post_delete, sender="core.AnalysisResult")
@receiver(post_delete, sender="geo.VehicleTrip")
@receiver(post_delete, sender="core.AnalysisFinding")
@receiver(post_delete, sender="core.ExtractedPassage")
def derived_removed(sender, instance, **kwargs):
    surface = {
        "extractedpassage": "passage",
        "analysisfinding": "finding",
        "analysisresult": "report",
        "vehicletrip": "trip",
    }[sender._meta.model_name]
    changed(
        instance.case_id,
        surface,
        "possible-trip" if surface == "trip" else "",
        [instance.pk],
    )


@receiver(post_save, sender="geo.VehicleTripGeneration")
def trips_changed(sender, instance, raw=False, **kwargs):
    if not raw and instance.finished_at:
        # Generation publication switches which older trips are visible.
        reconcile(instance.case_id)
