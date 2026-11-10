"""Finding pages and source links share one zero-based index convention."""

from django.urls import reverse

FINDINGS_PER_PAGE = 50


def finding_url(case_id, run_id, finding_index):
    url = reverse("core:analysis:detail", kwargs={"case_id": case_id, "run_id": run_id})
    return (
        f"{url}?page={finding_index // FINDINGS_PER_PAGE + 1}"
        f"&finding={finding_index + 1}#finding-{finding_index + 1}"
    )
