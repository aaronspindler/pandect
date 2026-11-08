"""Original-file cleanup shared by saves, imports, and committed deletions."""

import hashlib
import json
import logging
from contextlib import contextmanager
from functools import partial
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction

logger = logging.getLogger("core.storage")


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


@contextmanager
def retain_uploaded_originals():
    """Remove new upload files when a later write rolls back their transaction."""
    originals = []
    save_finished = False
    try:
        with transaction.atomic():
            yield originals
            # Once the body finishes, a failed commit acknowledgement may be ambiguous.
            save_finished = True
    except Exception:
        if not save_finished:
            for original in originals:
                delete_original(original.storage, original.name)
        raise


def original_upload_path(instance, filename):
    """Keep user filenames out of private, case-scoped storage keys."""
    return (
        f"evidence/cases/{instance.case_id}/{instance._meta.model_name}/{uuid4().hex}"
    )


def file_integrity(original, declared_content_type=""):
    """Inspect a new file without changing its position or trusting metadata."""
    if not original or original._committed:
        raise ValidationError({"file": "Supply a new named file, not a storage path."})
    content = original.file
    position = content.tell()
    digest = hashlib.sha256()
    byte_size = 0
    try:
        content.seek(0)
        while chunk := content.read(64 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    finally:
        content.seek(position)
    if not byte_size:
        raise ValidationError({"file": "The original file cannot be empty."})
    return {
        "sha256": digest.hexdigest(),
        "byte_size": byte_size,
        "original_filename": original.name.replace("\\", "/").rsplit("/", 1)[-1],
        "declared_content_type": (
            getattr(content, "content_type", None) or declared_content_type or ""
        ),
    }


def canonical_bytes(data):
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def schedule_original_deletion(sender, instance, using, **kwargs):
    if instance.file:
        transaction.on_commit(
            partial(delete_original, instance.file.storage, instance.file.name),
            using=using,
            robust=True,
        )
