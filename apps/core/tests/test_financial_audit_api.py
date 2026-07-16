from datetime import timedelta
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.core.models import FinancialAuditLog, FinancialEvent
from apps.core.services.financial_audit import log_financial_event
from apps.users.models import User, UserType
from apps.users.tokens import issue_tokens


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user)['access']}")
    return client


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password="pw", user_type="doctor")


@pytest.fixture
def other_doctor(db):
    return User.objects.create_user(email="other@example.com", password="pw", user_type="doctor")


@pytest.fixture
def nurse(db):
    return User.objects.create_user(
        email="nurse@example.com", phone_number="+911234567890", password="pw", user_type="nurse"
    )


@pytest.fixture
def clinic(owner, nurse):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=owner)
    ClinicStaffMembership.objects.create(clinic=clinic, user=owner, role=UserType.CLINIC_ADMIN)
    ClinicStaffMembership.objects.create(clinic=clinic, user=nurse, role=UserType.NURSE)
    return clinic


@pytest.mark.django_db
def test_default_view_shows_only_the_callers_own_actions(owner, other_doctor, clinic):
    log_financial_event(
        None,
        FinancialEvent.ENTRY_CREATED,
        actor=owner,
        object_type="revenue_entry",
        object_id="11111111-1111-1111-1111-111111111111",
        clinic=clinic,
        amount=Decimal("500.00"),
    )
    log_financial_event(
        None,
        FinancialEvent.ENTRY_CREATED,
        actor=other_doctor,
        object_type="revenue_entry",
        object_id="22222222-2222-2222-2222-222222222222",
        amount=Decimal("300.00"),
    )

    client = _authed_client(owner)
    response = client.get("/api/v1/core/financial-audit/")
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["object_id"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.django_db
def test_clinic_scoped_view_requires_admin_role(owner, nurse, clinic):
    log_financial_event(
        None,
        FinancialEvent.PAYMENT_POSTED,
        actor=owner,
        object_type="payment",
        object_id="33333333-3333-3333-3333-333333333333",
        clinic=clinic,
        amount=Decimal("500.00"),
    )

    admin_client = _authed_client(owner)
    response = admin_client.get(f"/api/v1/core/financial-audit/?clinic={clinic.external_id}")
    assert response.status_code == 200
    assert response.data["count"] == 1

    nurse_client = _authed_client(nurse)
    denied = nurse_client.get(f"/api/v1/core/financial-audit/?clinic={clinic.external_id}")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_filters_by_event_and_object_type(owner, clinic):
    log_financial_event(
        None,
        FinancialEvent.INVOICE_ISSUED,
        actor=owner,
        object_type="invoice",
        object_id="44444444-4444-4444-4444-444444444444",
        clinic=clinic,
        amount=Decimal("1000.00"),
    )
    log_financial_event(
        None,
        FinancialEvent.PAYMENT_POSTED,
        actor=owner,
        object_type="payment",
        object_id="55555555-5555-5555-5555-555555555555",
        clinic=clinic,
        amount=Decimal("1000.00"),
    )

    client = _authed_client(owner)
    response = client.get(
        f"/api/v1/core/financial-audit/?clinic={clinic.external_id}&event=invoice_issued"
    )
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["object_type"] == "invoice"

    response2 = client.get(
        f"/api/v1/core/financial-audit/?clinic={clinic.external_id}&object_type=payment"
    )
    assert response2.data["count"] == 1
    assert response2.data["results"][0]["object_type"] == "payment"


@pytest.mark.django_db
def test_date_range_filters(owner, clinic):
    log_financial_event(
        None,
        FinancialEvent.PAYMENT_POSTED,
        actor=owner,
        object_type="payment",
        object_id="66666666-6666-6666-6666-666666666666",
        clinic=clinic,
        amount=Decimal("100.00"),
    )

    log = FinancialAuditLog.objects.get()
    client = _authed_client(owner)

    future = (log.created_at + timedelta(days=1)).date().isoformat()
    response = client.get(
        f"/api/v1/core/financial-audit/?clinic={clinic.external_id}&from={future}"
    )
    assert response.data["count"] == 0

    today = log.created_at.date().isoformat()
    response2 = client.get(f"/api/v1/core/financial-audit/?clinic={clinic.external_id}&to={today}")
    assert response2.data["count"] == 1
