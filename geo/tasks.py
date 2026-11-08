"""Queue transport passes identifiers only; services reload authorized records."""

from django.tasks import task

from geo.vehicle_import_jobs import process_import


@task
def process_vehicle_import(import_id, job_token):
    return process_import(import_id, job_token)
