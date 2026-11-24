"""Queue transport passes identifiers only; services reload authorized records."""

from django.tasks import task

from geo.vehicle_import_jobs import process_import


@task
def process_vehicle_import(import_id, job_token):
    return process_import(import_id, job_token)


@task
def process_vehicle_trips(generation_id, token):
    from geo.trip_jobs import process_trips

    return process_trips(generation_id, token)


@task
def prepare_trip_addresses(generation_id, token):
    from geo.trip_jobs import prepare_addresses

    return prepare_addresses(generation_id, token)


@task
def retire_trip_snapshots(generation_id):
    from geo.trip_retention import retire_snapshots

    return retire_snapshots(generation_id)
