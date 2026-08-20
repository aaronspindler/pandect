"""Catalogue item processing and operator-decision routes."""

from django.urls import path

from pandect.processing import views

urlpatterns = [
    path("<uuid:item_id>/", views.item_detail, name="catalogue-item-detail"),
    path(
        "<uuid:item_id>/processing/status/",
        views.processing_status,
        name="catalogue-item-processing-status",
    ),
    path(
        "<uuid:item_id>/processing/rerun/",
        views.rerun_local_processing,
        name="catalogue-item-rerun",
    ),
    path(
        "<uuid:item_id>/processing/stages/<uuid:stage_id>/retry/",
        views.retry_failed_stage,
        name="catalogue-item-retry-stage",
    ),
    path(
        "<uuid:item_id>/timestamps/<uuid:claim_id>/promote/",
        views.promote_timestamp,
        name="catalogue-item-promote-timestamp",
    ),
    path(
        "<uuid:item_id>/subjects/create/",
        views.create_subject,
        name="catalogue-item-create-subject",
    ),
    path(
        "<uuid:item_id>/observations/<uuid:observation_id>/reference/",
        views.add_subject_reference,
        name="catalogue-item-add-reference",
    ),
    path(
        "<uuid:item_id>/observations/<uuid:observation_id>/reference/update/",
        views.update_subject_reference,
        name="catalogue-item-update-reference",
    ),
    path(
        "<uuid:item_id>/observations/<uuid:observation_id>/reference/remove/",
        views.remove_subject_reference,
        name="catalogue-item-remove-reference",
    ),
    path(
        "<uuid:item_id>/candidates/<uuid:candidate_id>/decide/",
        views.decide_candidate,
        name="catalogue-item-decide-candidate",
    ),
    path(
        "<uuid:item_id>/processing/remote/consent/",
        views.consent_remote_processing,
        name="catalogue-item-remote-consent",
    ),
    path(
        "<uuid:item_id>/processing/remote/followup/consent/",
        views.consent_remote_followup,
        name="catalogue-item-remote-followup-consent",
    ),
]
