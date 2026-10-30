"""Original-file cleanup shared by saves, imports, and committed deletions."""

import logging
from contextlib import contextmanager

from django.db import transaction

logger = logging.getLogger("evidence.signals")


def delete_original(storage, name):
    try:
        storage.delete(name)
    except Exception:
        # Generated keys support manual retry without logging original filenames,
        # file contents, or potentially sensitive storage exception messages.
        logger.error(
            "Evidence file cleanup failed; retry deleting storage key %s", name
        )


@contextmanager
def new_file_save(original, *, using):
    """Remove a newly written file when its model save fails and rolls back.

    A later rollback of a caller's outer transaction remains that caller's
    responsibility. A commit failure has an uncertain outcome, so retain its file.
    """
    if not original or original._committed:
        yield
        return
    save_finished = False
    try:
        with transaction.atomic(using=using):
            yield
            save_finished = True
    except Exception:
        if not save_finished and original._committed:
            delete_original(original.storage, original.name)
        raise
