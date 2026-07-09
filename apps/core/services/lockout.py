"""
Account lockout service — HIPAA § 164.308(a)(5)(ii)(D).

Tracks failed login attempts per user and locks the account for a
configurable window once the threshold is crossed. State lives on the
User row itself (failed_login_attempts, locked_until) so it survives
restarts and is visible in admin without needing a separate table.

Settings (all in base.py):
  LOCKOUT_MAX_ATTEMPTS   — failures before lockout (default: 5)
  LOCKOUT_DURATION_MINS  — how long the lock lasts  (default: 30)
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _max_attempts() -> int:
    return getattr(settings, "LOCKOUT_MAX_ATTEMPTS", 5)


def _duration_mins() -> int:
    return getattr(settings, "LOCKOUT_DURATION_MINS", 30)


def record_failed_attempt(user) -> bool:
    """
    Increment the failed-login counter for `user`. If the threshold is
    reached, lock the account and return True. Otherwise return False.

    Uses a F()-expression update + refresh to avoid a race condition where
    two simultaneous failures both read 4, both write 5, and both lock —
    which is fine (idempotent), but we want the return value to be accurate.
    """
    from django.apps import apps
    from django.db.models import F

    User = apps.get_model("users", "User")
    User.objects.filter(pk=user.pk).update(failed_login_attempts=F("failed_login_attempts") + 1)
    user.refresh_from_db(fields=["failed_login_attempts", "locked_until"])

    if user.failed_login_attempts >= _max_attempts():
        locked_until = timezone.now() + timedelta(minutes=_duration_mins())
        User.objects.filter(pk=user.pk).update(locked_until=locked_until)
        user.locked_until = locked_until
        logger.warning(
            "Account locked: user_id=%s email=%s until=%s",
            user.pk,
            user.email,
            locked_until.isoformat(),
        )
        return True

    return False


def clear_failed_attempts(user) -> None:
    """
    Reset the counter and lockout on a successful login. Called after
    credentials are verified but before the token is issued.
    """
    from django.apps import apps

    User = apps.get_model("users", "User")
    User.objects.filter(pk=user.pk).update(failed_login_attempts=0, locked_until=None)
    user.failed_login_attempts = 0
    user.locked_until = None
