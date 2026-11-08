"""Queue transport; case and evidence contents are reloaded in domain services."""

from django.tasks import task

from core.analysis.services import process_run


@task
def process_analysis(run_id):
    return process_run(run_id)
