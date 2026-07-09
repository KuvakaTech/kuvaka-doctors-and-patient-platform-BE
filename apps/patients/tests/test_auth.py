import pytest
from rest_framework.test import APIClient

from apps.users.models import EmailOTP, OTPPurpose, User


@pytest.fixture
def client():
    return APIClient()


# --- Path 1: direct email + password signup ---------------------------------


@pytest.mark.django_db
def test_register_creates_patient_with_password(client):
    response = client.post(
        "/api/v1/patients/auth/register/",
        {"email": "patient@example.com", "password": "S3curePass!23", "full_name": "Jane Roe"},
    )
    assert response.status_code == 201

    user = User.objects.get(email="patient@example.com")
    assert user.user_type == "patient"
    assert user.email_verified is False
    assert user.has_usable_password() is True
    assert hasattr(user, "patient_profile")


@pytest.mark.django_db
def test_login_blocked_until_email_verified(client):
    client.post(
        "/api/v1/patients/auth/register/",
        {"email": "patient@example.com", "password": "S3curePass!23"},
    )
    response = client.post(
        "/api/v1/patients/auth/login/",
        {"email": "patient@example.com", "password": "S3curePass!23"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_verify_email_then_password_login_succeeds(client):
    client.post(
        "/api/v1/patients/auth/register/",
        {"email": "patient@example.com", "password": "S3curePass!23"},
    )
    user = User.objects.get(email="patient@example.com")
    _otp, code = EmailOTP.issue(user, OTPPurpose.EMAIL_VERIFICATION)

    verify_response = client.post(
        "/api/v1/patients/auth/verify-email/", {"email": "patient@example.com", "code": code}
    )
    assert verify_response.status_code == 200

    login_response = client.post(
        "/api/v1/patients/auth/login/",
        {"email": "patient@example.com", "password": "S3curePass!23"},
    )
    assert login_response.status_code == 200
    assert "access" in login_response.data


# --- Path 2: passwordless email-OTP onboarding ------------------------------


@pytest.mark.django_db
def test_otp_request_creates_patient_lazily(client):
    response = client.post(
        "/api/v1/patients/auth/otp/request/",
        {"email": "patient@example.com", "full_name": "Jane Roe"},
    )
    assert response.status_code == 200

    user = User.objects.get(email="patient@example.com")
    assert user.user_type == "patient"
    assert user.email_verified is False
    assert user.has_usable_password() is False
    assert hasattr(user, "patient_profile")
    assert EmailOTP.objects.filter(user=user, purpose=OTPPurpose.LOGIN).exists()


@pytest.mark.django_db
def test_otp_request_does_not_duplicate_existing_patient(client):
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    assert User.objects.filter(email="patient@example.com").count() == 1


@pytest.mark.django_db
def test_otp_request_rejects_email_registered_as_doctor(client):
    User.objects.create_user(email="shared@example.com", password="pass1234", user_type="doctor")
    response = client.post("/api/v1/patients/auth/otp/request/", {"email": "shared@example.com"})
    assert response.status_code == 400


@pytest.mark.django_db
def test_otp_verify_with_correct_code_issues_tokens_and_marks_verified(client):
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    user = User.objects.get(email="patient@example.com")
    _otp, code = EmailOTP.issue(user, OTPPurpose.LOGIN)

    response = client.post(
        "/api/v1/patients/auth/otp/verify/", {"email": "patient@example.com", "code": code}
    )
    assert response.status_code == 200
    assert "access" in response.data
    assert response.data["password_set"] is False

    user.refresh_from_db()
    assert user.email_verified is True


@pytest.mark.django_db
def test_otp_verify_rejects_wrong_code(client):
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    response = client.post(
        "/api/v1/patients/auth/otp/verify/", {"email": "patient@example.com", "code": "000000"}
    )
    assert response.status_code == 400


# --- Setting a password after OTP onboarding, to avoid repeat OTP emails ----


@pytest.mark.django_db
def test_set_password_after_otp_then_password_login_works(client):
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    user = User.objects.get(email="patient@example.com")
    _otp, code = EmailOTP.issue(user, OTPPurpose.LOGIN)
    verify_response = client.post(
        "/api/v1/patients/auth/otp/verify/", {"email": "patient@example.com", "code": code}
    )
    access_token = verify_response.data["access"]

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    set_password_response = client.post(
        "/api/v1/patients/auth/set-password/", {"password": "NewSecurePass!1"}
    )
    assert set_password_response.status_code == 200

    client.credentials()  # clear auth header
    login_response = client.post(
        "/api/v1/patients/auth/login/",
        {"email": "patient@example.com", "password": "NewSecurePass!1"},
    )
    assert login_response.status_code == 200
    assert "access" in login_response.data


@pytest.mark.django_db
def test_set_password_requires_authentication(client):
    response = client.post("/api/v1/patients/auth/set-password/", {"password": "NewSecurePass!1"})
    assert response.status_code == 401


@pytest.mark.django_db
def test_login_rejects_password_when_none_set_yet(client):
    """An OTP-only patient can't have their (nonexistent) password brute-forced via login."""
    client.post("/api/v1/patients/auth/otp/request/", {"email": "patient@example.com"})
    response = client.post(
        "/api/v1/patients/auth/login/",
        {"email": "patient@example.com", "password": "anything"},
    )
    assert response.status_code == 401
