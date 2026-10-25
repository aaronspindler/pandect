from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Pandect's user model, configured before the first database migration."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
