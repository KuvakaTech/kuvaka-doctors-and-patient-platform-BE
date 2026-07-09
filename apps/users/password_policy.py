"""
User-aware password policy check.

`serializers.CharField(validators=[validate_password])` only ever calls
`validate_password(value)` with no `user` argument — DRF field validators
are invoked with just the field's value. That silently disables the two
validators that need a user to do anything (`PasswordHistoryValidator`,
Django's `UserAttributeSimilarityValidator`), since both no-op when
`user is None`. The length/common-password/numeric validators still work
without a user, which is why this went unnoticed.

Call `validate_new_password(password, user)` explicitly in views once the
target user is known (password reset confirm, change-password, set-password)
to actually get the user-aware checks.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


def validate_new_password(password: str, user, *, field_name: str = "new_password") -> None:
    """Raises rest_framework.serializers.ValidationError on policy violation."""
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field_name: list(exc.messages)}) from exc
