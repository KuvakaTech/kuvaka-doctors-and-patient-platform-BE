"""
Custom Django password validators for HIPAA compliance.

Plugged into AUTH_PASSWORD_VALIDATORS in settings. Each class follows the
standard Django validator interface: validate(password, user=None) raises
ValidationError on failure, and get_help_text() returns a user-facing string.
"""

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class PasswordHistoryValidator:
    """
    Prevents reuse of the last N passwords.

    HIPAA § 164.308(a)(5)(ii)(D) addressable — reduces risk that a
    compromised credential stays usable after a forced rotation.

    Setting: PASSWORD_HISTORY_COUNT (default: 5)
    """

    def __init__(self):
        self.history_count = getattr(settings, "PASSWORD_HISTORY_COUNT", 5)

    def validate(self, password: str, user=None) -> None:
        if user is None or not user.pk:
            # New account — no history to check yet
            return

        # Import here to avoid circular import at module load time
        from apps.users.models import PasswordHistory

        recent = PasswordHistory.objects.filter(user=user).order_by("-created_at")[
            : self.history_count
        ]
        for entry in recent:
            if check_password(password, entry.password_hash):
                raise ValidationError(
                    _(
                        f"You cannot reuse any of your last {self.history_count} passwords. "
                        "Please choose a different password."
                    ),
                    code="password_recently_used",
                )

    def get_help_text(self) -> str:
        return _(
            f"Your password cannot be the same as any of your last {self.history_count} passwords."
        )


class MinimumLengthValidator:
    """
    Enforces a minimum length of 12 characters (overrides Django's default 8).

    NIST SP 800-63B recommends at least 8 but allows up to the implementer.
    12 is a common HIPAA-oriented baseline.

    Setting: PASSWORD_MIN_LENGTH (default: 12)
    """

    def __init__(self):
        self.min_length = getattr(settings, "PASSWORD_MIN_LENGTH", 12)

    def validate(self, password: str, user=None) -> None:
        if len(password) < self.min_length:
            raise ValidationError(
                _(f"Your password must be at least {self.min_length} characters long."),
                code="password_too_short",
                params={"min_length": self.min_length},
            )

    def get_help_text(self) -> str:
        return _(f"Your password must be at least {self.min_length} characters long.")
