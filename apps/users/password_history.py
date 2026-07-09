"""
Helpers for maintaining the PasswordHistory table.

Call `record_password_change(user)` immediately after `user.set_password()`
and before `user.save()` so the *new* hash (already set on the user instance
but not yet persisted) is captured.
"""

from django.conf import settings


def record_password_change(user) -> None:
    """
    Save the user's current (newly set) password hash to PasswordHistory
    and prune old entries beyond PASSWORD_HISTORY_COUNT.

    Must be called after set_password() but the timing relative to save()
    doesn't matter — we read user.password which is already updated in memory.
    """
    from apps.users.models import PasswordHistory

    history_count = getattr(settings, "PASSWORD_HISTORY_COUNT", 5)

    PasswordHistory.objects.create(user=user, password_hash=user.password)

    # Prune: keep only the most recent `history_count` entries
    keep_ids = (
        PasswordHistory.objects.filter(user=user)
        .order_by("-created_at")
        .values_list("id", flat=True)[:history_count]
    )
    PasswordHistory.objects.filter(user=user).exclude(id__in=list(keep_ids)).delete()
