"""Bound multipart evidence uploads before temporary files grow."""

from django.conf import settings
from django.core.files.uploadhandler import StopUpload, TemporaryFileUploadHandler
from django.http import HttpResponse

from pandect.core.storage import StorageCapacityError, require_storage_space


class UploadLimitError(ValueError):
    """An upload exceeded a configured byte or count limit."""


def validate_upload_sizes(uploads) -> None:
    """Reject oversized already-materialized uploads at the service boundary.

    HTTP intake and original-file copying also count actual bytes; this check
    is not a substitute for either streaming limit.
    """
    if len(uploads) > settings.UPLOAD_MAX_FILES:
        raise UploadLimitError(
            f"Add no more than {settings.UPLOAD_MAX_FILES} files at a time."
        )
    if any(upload.size > settings.UPLOAD_MAX_FILE_BYTES for upload in uploads):
        raise UploadLimitError(
            f"Each file must be at most {settings.UPLOAD_MAX_FILE_BYTES:,} bytes."
        )
    if sum(upload.size for upload in uploads) > settings.UPLOAD_MAX_BATCH_BYTES:
        raise UploadLimitError(
            f"One batch must be at most {settings.UPLOAD_MAX_BATCH_BYTES:,} bytes."
        )


class GuardedUploadHandler(TemporaryFileUploadHandler):
    """The first and only handler; no upload bytes enter memory-file buffering."""

    chunk_size = 64 * 1024

    def __init__(self, request=None):
        super().__init__(request)
        self.file_count = 0
        self.file_bytes = 0
        self.batch_bytes = 0

    def reject(self, message: str, status: int) -> None:
        self.request.upload_rejection = (message, status)
        # Django closes the current file on StopUpload. The middleware closes
        # completed files and rejects the entire request, never a partial batch.
        raise StopUpload(connection_reset=True)

    def new_file(self, *args, **kwargs):
        self.file_count += 1
        self.file_bytes = 0
        if self.file_count > settings.UPLOAD_MAX_FILES:
            self.reject(
                f"Add no more than {settings.UPLOAD_MAX_FILES} files at a time.", 413
            )
        try:
            require_storage_space(settings.FILE_UPLOAD_TEMP_DIR)
            super().new_file(*args, **kwargs)
        except StorageCapacityError as error:
            self.reject(str(error), 507)
        except OSError:
            self.reject(
                "The upload could not be stored. Check the data volume's "
                "free space and permissions, then retry.",
                507,
            )

    def receive_data_chunk(self, raw_data, start):
        self.file_bytes += len(raw_data)
        self.batch_bytes += len(raw_data)
        if self.file_bytes > settings.UPLOAD_MAX_FILE_BYTES:
            self.reject(
                f"Each file must be at most {settings.UPLOAD_MAX_FILE_BYTES:,} bytes.",
                413,
            )
        if self.batch_bytes > settings.UPLOAD_MAX_BATCH_BYTES:
            self.reject(
                f"One batch must be at most {settings.UPLOAD_MAX_BATCH_BYTES:,} bytes.",
                413,
            )
        try:
            require_storage_space(settings.FILE_UPLOAD_TEMP_DIR, len(raw_data))
            return super().receive_data_chunk(raw_data, start)
        except StorageCapacityError as error:
            self.reject(str(error), 507)
        except OSError:
            self.reject(
                "The upload could not be stored. Check the data volume's "
                "free space and permissions, then retry.",
                507,
            )

    def file_complete(self, file_size):
        try:
            return super().file_complete(file_size)
        except OSError:
            self.reject(
                "The upload could not be stored. Check the data volume's "
                "free space and permissions, then retry.",
                507,
            )


class UploadRejectionMiddleware:
    """Parse before CSRF so a stopped multipart request has a useful response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.content_type == "multipart/form-data":
            # Accessing FILES parses the multipart stream exactly once. Never
            # access request.body, which would buffer the complete upload.
            files = request.FILES
            rejection = getattr(request, "upload_rejection", None)
            if rejection is not None:
                for field in files:
                    for upload in files.getlist(field):
                        upload.close()
                message, status = rejection
                return HttpResponse(
                    f"{message} No items from this upload were added.",
                    status=status,
                    content_type="text/plain; charset=utf-8",
                )
        return self.get_response(request)
