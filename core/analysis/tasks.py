"""Queue transport; case and evidence contents are reloaded in domain services."""

from django.tasks import task

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.services import process_run


@task
def process_analysis(run_id):
    return process_run(run_id)


@task
def prepare_case_analysis(batch_id):
    return prepare_case_analysis_batch(batch_id)
