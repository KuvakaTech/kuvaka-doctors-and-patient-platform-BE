import pytest
from rest_framework.test import APIClient

from apps.users.models import EmailOTP, OTPPurpose, User


@pytest.fixture
def client():
    return APIClient()


# Fields required by DoctorRegisterSerializer beyond email/password.
_REQUIRED_REGISTER_FIELDS = {"first_name": "Jane", "last_name": "Doe", "terms_accepted": True}


@pytest.mark.django_db
def test_register_creates_unverified_user_and_sends_otp(client):
    response = client.post(
        "/api/v1/doctors/auth/register/",
        {
            "email": "doc@example.com",
            "password": "S3curePass!23",
            **_REQUIRED_REGISTER_FIELDS,
        },
    )
    assert response.status_code == 201

    user = User.objects.get(email="doc@example.com")
    assert user.user_type == "doctor"
    assert user.email_verified is False
    assert user.full_name == "Jane Doe"
    assert hasattr(user, "doctor_profile")
    assert user.doctor_profile.terms_accepted_at is not None
    assert EmailOTP.objects.filter(user=user, purpose=OTPPurpose.EMAIL_VERIFICATION).exists()


@pytest.mark.django_db
def test_register_requires_terms_acceptance(client):
    response = client.post(
        "/api/v1/doctors/auth/register/",
        {
            "email": "doc@example.com",
            "password": "S3curePass!23",
            "first_name": "Jane",
            "last_name": "Doe",
            "terms_accepted": False,
        },
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_register_rejects_duplicate_email(client):
    User.objects.create_user(email="doc@example.com", password="pass1234", user_type="doctor")
    response = client.post(
        "/api/v1/doctors/auth/register/",
        {"email": "doc@example.com", "password": "S3curePass!23", **_REQUIRED_REGISTER_FIELDS},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_login_blocked_until_email_verified(client):
    client.post(
        "/api/v1/doctors/auth/register/",
        {"email": "doc@example.com", "password": "S3curePass!23", **_REQUIRED_REGISTER_FIELDS},
    )
    response = client.post(
        "/api/v1/doctors/auth/login/",
        {"email": "doc@example.com", "password": "S3curePass!23"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_verify_email_then_login_succeeds(client):
    client.post(
        "/api/v1/doctors/auth/register/",
        {"email": "doc@example.com", "password": "S3curePass!23", **_REQUIRED_REGISTER_FIELDS},
    )
    user = User.objects.get(email="doc@example.com")

    # The register endpoint already issued an OTP and emailed the plaintext
    # code, which we can't intercept here as an HTTP client — re-issue
    # directly against the model to get a code we can assert against.
    _otp, code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)

    verify_response = client.post(
        "/api/v1/doctors/auth/verify-email/", {"email": "doc@example.com", "code": code}
    )
    assert verify_response.status_code == 200
    assert "access" in verify_response.data

    user.refresh_from_db()
    assert user.email_verified is True

    login_response = client.post(
        "/api/v1/doctors/auth/login/",
        {"email": "doc@example.com", "password": "S3curePass!23"},
    )
    assert login_response.status_code == 200
    assert "access" in login_response.data


@pytest.mark.django_db
def test_login_rejects_wrong_password(client):
    client.post(
        "/api/v1/doctors/auth/register/",
        {"email": "doc@example.com", "password": "S3curePass!23", **_REQUIRED_REGISTER_FIELDS},
    )
    response = client.post(
        "/api/v1/doctors/auth/login/", {"email": "doc@example.com", "password": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_password_reset_flow(client):
    client.post(
        "/api/v1/doctors/auth/register/",
        {
            "email": "doc@example.com",
            "password": "OldSecurePass!234",
            **_REQUIRED_REGISTER_FIELDS,
        },
    )
    user = User.objects.get(email="doc@example.com")
    _otp, code = EmailOTP.issue(user, OTPPurpose.PASSWORD_RESET)

    response = client.post(
        "/api/v1/doctors/auth/password-reset/confirm/",
        {"email": "doc@example.com", "code": code, "new_password": "NewSecurePass!456"},
    )
    assert response.status_code == 200

    user.refresh_from_db()
    assert user.check_password("NewSecurePass!456")


@pytest.mark.django_db
def test_password_reset_request_does_not_leak_registered_emails(client):
    response = client.post(
        "/api/v1/doctors/auth/password-reset/request/", {"email": "nobody@example.com"}
    )
    assert response.status_code == 200
