from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Pandect's user model, configured before the first database migration."""
