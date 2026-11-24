"""Original-file cleanup shared by saves, imports, and committed deletions."""

import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("core.storage")
_staged_upload = ContextVar("pandect_staged_upload", default=None)


def delete_original(storage, name):
    try:
        storage.delete(name)
    except FileNotFoundError:
        pass  # A retry can follow a crash after the storage operation succeeded.
    except Exception:
        # Generated keys support manual retry without logging original filenames,
        # file contents, or potentially sensitive storage exception messages.
        logger.error(
            "Evidence file cleanup failed; retry deleting storage key %s", name
        )
        return False
    return True


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


def _measure_file(content):
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
    return digest.hexdigest(), byte_size


@contextmanager
def staged_file_upload(upload, *, limit):
    """Measure a request-owned upload once, before taking its case write lock."""
    if not isinstance(upload, UploadedFile) or not 0 < upload.size <= limit:
        yield None
        return
    measured = _measure_file(upload)
    token = _staged_upload.set((upload, measured))
    try:
        yield measured
    finally:
        _staged_upload.reset(token)


def file_integrity(original, declared_content_type=""):
    """Inspect a new file without changing its position or trusting metadata."""
    if not original or original._committed:
        raise ValidationError({"file": "Supply a new named file, not a storage path."})
    content = original.file
    staged = _staged_upload.get()
    checksum, byte_size = (
        staged[1]
        if staged is not None and staged[0] is content
        else _measure_file(content)
    )
    if not byte_size:
        raise ValidationError({"file": "The original file cannot be empty."})
    return {
        "sha256": checksum,
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
        from core.models import PendingFileDeletion

        pending = PendingFileDeletion.objects.using(using).create(
            storage_key=instance.file.name
        )
        transaction.on_commit(
            partial(complete_file_deletion, pending, using, instance.file.storage),
            using=using,
            robust=True,
        )


def complete_file_deletion(pending, using, storage=default_storage):
    if not delete_original(storage, pending.storage_key):
        type(pending).objects.using(using).filter(pk=pending.pk).update(
            last_attempted_at=timezone.now()
        )
        return False
    pending.delete(using=using)
    return True
