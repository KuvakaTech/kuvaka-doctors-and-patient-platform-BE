from datetime import timedelta

import pytest
from django.utils import timezone

from apps.users.models import EmailOTP, OTPPurpose, User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="doc@example.com", password="pass1234", user_type="doctor"
    )


@pytest.mark.django_db
def test_issue_and_verify_correct_code(user):
    otp, code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)
    assert otp.verify_code(code) is True
    otp.refresh_from_db()
    assert otp.consumed_at is not None


@pytest.mark.django_db
def test_wrong_code_does_not_consume(user):
    otp, _ = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)
    assert otp.verify_code("000000") is False
    otp.refresh_from_db()
    assert otp.consumed_at is None
    assert otp.attempts == 1


@pytest.mark.django_db
def test_expired_code_rejected(user):
    otp, code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)
    otp.expires_at = timezone.now() - timedelta(seconds=1)
    otp.save(update_fields=["expires_at"])
    assert otp.verify_code(code) is False


@pytest.mark.django_db
def test_max_attempts_locks_out(user, settings):
    settings.OTP_MAX_ATTEMPTS = 2
    otp, code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)
    assert otp.verify_code("000000") is False
    assert otp.verify_code("000000") is False
    # third attempt is beyond the limit, even with the correct code
    assert otp.verify_code(code) is False


@pytest.mark.django_db
def test_issuing_new_otp_invalidates_prior_unconsumed_one(user):
    first, first_code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)
    _second, second_code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)

    first.refresh_from_db()
    assert first.consumed_at is not None
    assert first.verify_code(first_code) is False
