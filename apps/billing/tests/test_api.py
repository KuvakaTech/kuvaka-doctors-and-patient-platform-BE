from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.models import ChargeItemDefinition, InvoiceStatus, Payment
from apps.billing.services import (
    capture_charge,
    create_draft_invoice,
    get_or_create_account,
    issue_invoice,
)
from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.patients.models import PatientProfile
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
def clinic(owner):
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=owner)
    ClinicStaffMembership.objects.create(clinic=clinic, user=owner, role=UserType.CLINIC_ADMIN)
    return clinic


@pytest.fixture
def billing_nurse(clinic):
    nurse = User.objects.create_user(email="nurse@example.com", password="pw", user_type="nurse")
    ClinicStaffMembership.objects.create(
        clinic=clinic, user=nurse, role=UserType.NURSE, permissions=["manage_billing"]
    )
    return nurse


@pytest.fixture
def refund_nurse(clinic):
    nurse = User.objects.create_user(email="refunds@example.com", password="pw", user_type="nurse")
    ClinicStaffMembership.objects.create(
        clinic=clinic,
        user=nurse,
        role=UserType.NURSE,
        permissions=["manage_billing", "manage_refunds"],
    )
    return nurse


@pytest.fixture
def plain_nurse(clinic):
    nurse = User.objects.create_user(email="plain@example.com", password="pw", user_type="nurse")
    ClinicStaffMembership.objects.create(clinic=clinic, user=nurse, role=UserType.NURSE)
    return nurse


@pytest.fixture
def other_doctor(clinic):
    doc = User.objects.create_user(
        email="other-doc@example.com", password="pw", user_type="doctor"
    )
    ClinicStaffMembership.objects.create(clinic=clinic, user=doc, role=UserType.DOCTOR)
    return doc


@pytest.fixture
def patient_profile(db):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


@pytest.fixture
def account(patient_profile, clinic):
    return get_or_create_account(patient=patient_profile, clinic=clinic)


def _def_url(clinic):
    return f"/api/v1/billing/clinics/{clinic.external_id}/definitions/"


def _account_url(clinic, account, suffix=""):
    return f"/api/v1/billing/clinics/{clinic.external_id}/accounts/{account.external_id}/{suffix}"


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_doctor_creates_own_consultation_fee(other_doctor, clinic):
    client = _authed_client(other_doctor)
    response = client.post(
        _def_url(clinic),
        {
            "code": "opd-consult",
            "title": "OPD Consultation",
            "category": "consultation",
            "price_components": [{"type": "base", "amount": "450.00"}],
            "doctor_scoped": True,
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    definition = ChargeItemDefinition.objects.get(clinic=clinic, doctor=other_doctor)
    assert definition.price_components[0]["amount"] == "450.00"


@pytest.mark.django_db
def test_nurse_cannot_create_doctor_scoped_fee(plain_nurse, clinic):
    client = _authed_client(plain_nurse)
    response = client.post(
        _def_url(clinic),
        {
            "code": "opd-consult",
            "title": "X",
            "category": "consultation",
            "price_components": [{"type": "base", "amount": "100.00"}],
            "doctor_scoped": True,
        },
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_admin_creates_clinic_wide_definition(owner, clinic):
    client = _authed_client(owner)
    response = client.post(
        _def_url(clinic),
        {
            "code": "dressing",
            "title": "Wound Dressing",
            "category": "service",
            "price_components": [{"type": "base", "amount": "150.00"}],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["doctor"] is None


@pytest.mark.django_db
def test_non_admin_cannot_create_clinic_wide_definition(plain_nurse, clinic):
    client = _authed_client(plain_nurse)
    response = client.post(
        _def_url(clinic),
        {
            "code": "dressing",
            "title": "Wound Dressing",
            "category": "service",
            "price_components": [{"type": "base", "amount": "150.00"}],
        },
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_price_change_creates_new_version(owner, clinic):
    client = _authed_client(owner)
    create_response = client.post(
        _def_url(clinic),
        {
            "code": "dressing",
            "title": "Wound Dressing",
            "category": "service",
            "price_components": [{"type": "base", "amount": "150.00"}],
        },
        format="json",
    )
    external_id = create_response.data["external_id"]
    patch_response = client.patch(
        f"{_def_url(clinic)}{external_id}/",
        {"price_components": [{"type": "base", "amount": "200.00"}]},
        format="json",
    )
    assert patch_response.status_code == 200
    assert patch_response.data["version"] == 2
    assert patch_response.data["external_id"] != external_id  # a new row, old one deactivated


# ---------------------------------------------------------------------------
# Accounts + charge items
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_billing_nurse_can_capture_charge(billing_nurse, clinic, account):
    client = _authed_client(billing_nurse)
    response = client.post(
        _account_url(clinic, account, "charge-items/"),
        {
            "title": "Consultation",
            "category": "consultation",
            "price_components": [{"type": "base", "amount": "500.00"}],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["status"] == "unbilled"


@pytest.mark.django_db
def test_plain_nurse_cannot_capture_charge(plain_nurse, clinic, account):
    client = _authed_client(plain_nurse)
    response = client.post(
        _account_url(clinic, account, "charge-items/"),
        {
            "title": "Consultation",
            "category": "consultation",
            "price_components": [{"type": "base", "amount": "500.00"}],
        },
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_unbilled_queue_filter(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "100.00"}],
        recorded_by=owner,
    )
    create_draft_invoice(
        account=account, charge_items=[charge]
    )  # detaches it from the un-billed queue... actually keeps status unbilled until issued

    client = _authed_client(billing_nurse)
    response = client.get(_account_url(clinic, account, "charge-items/?status=unbilled"))
    assert response.status_code == 200
    # Still shows as unbilled (attached to a draft, but not yet issued).
    assert response.data["count"] == 1


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_invoice_create_issue_flow(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    client = _authed_client(billing_nurse)
    create_response = client.post(
        _account_url(clinic, account, "invoices/"),
        {"charge_items": [str(charge.external_id)]},
        format="json",
    )
    assert create_response.status_code == 201, create_response.data
    invoice_id = create_response.data["external_id"]
    assert create_response.data["status"] == "draft"

    issue_response = client.post(_account_url(clinic, account, f"invoices/{invoice_id}/issue/"))
    assert issue_response.status_code == 200
    assert issue_response.data["status"] == "issued"
    assert issue_response.data["total_net"] == "500.00"


@pytest.mark.django_db
def test_billing_nurse_cannot_cancel_invoice_needs_refund_flag(
    billing_nurse, clinic, account, owner
):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(billing_nurse)
    response = client.post(
        _account_url(clinic, account, f"invoices/{invoice.external_id}/cancel/"),
        {"reason": "mistake"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_refund_nurse_can_cancel_invoice(refund_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(refund_nurse)
    response = client.post(
        _account_url(clinic, account, f"invoices/{invoice.external_id}/cancel/"),
        {"reason": "mistake"},
    )
    assert response.status_code == 200
    assert response.data["status"] == "cancelled"


@pytest.mark.django_db
def test_patient_can_view_own_invoice(clinic, account, owner, patient_profile):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(patient_profile.user)
    response = client.get(_account_url(clinic, account, f"invoices/{invoice.external_id}/"))
    assert response.status_code == 200
    assert response.data["number"] == invoice.number


@pytest.mark.django_db
def test_other_patient_cannot_view_invoice(clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    stranger = User.objects.create_user(
        email="stranger@example.com", password="pw", user_type="patient"
    )
    PatientProfile.objects.create(user=stranger)
    client = _authed_client(stranger)
    response = client.get(_account_url(clinic, account, f"invoices/{invoice.external_id}/"))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Payments + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_payment_requires_idempotency_key_header(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(billing_nurse)
    response = client.post(
        _account_url(clinic, account, "payments/"),
        {
            "kind": "payment",
            "invoice": str(invoice.external_id),
            "amount": "500.00",
            "method": "cash",
        },
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_payment_idempotency_replay_and_conflict(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(billing_nurse)
    body = {
        "kind": "payment",
        "invoice": str(invoice.external_id),
        "amount": "500.00",
        "method": "cash",
    }

    first = client.post(
        _account_url(clinic, account, "payments/"),
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-key-1",
    )
    assert first.status_code == 201
    assert Payment.objects.count() == 1

    replay = client.post(
        _account_url(clinic, account, "payments/"),
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-key-1",
    )
    assert replay.status_code == 201
    assert replay.data["external_id"] == first.data["external_id"]
    assert Payment.objects.count() == 1  # not double-posted

    conflict_body = {**body, "amount": "999.00"}
    conflict = client.post(
        _account_url(clinic, account, "payments/"),
        conflict_body,
        format="json",
        HTTP_IDEMPOTENCY_KEY="retry-key-1",
    )
    assert conflict.status_code == 422


@pytest.mark.django_db
def test_refund_requires_manage_refunds_flag(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    client = _authed_client(billing_nurse)  # manage_billing only, not manage_refunds
    response = client.post(
        _account_url(clinic, account, "payments/"),
        {
            "kind": "refund",
            "invoice": str(invoice.external_id),
            "amount": "100.00",
            "method": "cash",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="refund-key-1",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_apply_advance_endpoint(billing_nurse, clinic, account, owner):
    client = _authed_client(billing_nurse)
    client.post(
        _account_url(clinic, account, "payments/"),
        {"kind": "advance", "amount": "500.00", "method": "cash"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="advance-key-1",
    )
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )

    response = client.post(
        _account_url(clinic, account, "apply-advance/"),
        {"invoice": str(invoice.external_id), "amount": "500.00"},
        format="json",
    )
    assert response.status_code == 201, response.data
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reconciliation_requires_view_billing(plain_nurse, clinic):
    client = _authed_client(plain_nurse)
    response = client.get(f"/api/v1/billing/clinics/{clinic.external_id}/reconciliation/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_reconciliation_totals(billing_nurse, clinic, account, owner):
    charge = capture_charge(
        account=account,
        category="consultation",
        title="A",
        price_components=[{"type": "base", "amount": "500.00"}],
        recorded_by=owner,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=owner
    )
    client = _authed_client(billing_nurse)
    client.post(
        _account_url(clinic, account, "payments/"),
        {
            "kind": "payment",
            "invoice": str(invoice.external_id),
            "amount": "500.00",
            "method": "cash",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="recon-key-1",
    )
    response = client.get(f"/api/v1/billing/clinics/{clinic.external_id}/reconciliation/")
    assert response.status_code == 200
    assert Decimal(str(response.data["total"])) == Decimal("500.00")


# ---------------------------------------------------------------------------
# Patient-facing reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patient_my_accounts_only_own(account, patient_profile, clinic, owner):
    other_patient = User.objects.create_user(
        email="p2@example.com", password="pw", user_type="patient"
    )
    other_profile = PatientProfile.objects.create(user=other_patient)
    get_or_create_account(patient=other_profile, clinic=clinic)

    client = _authed_client(patient_profile.user)
    response = client.get("/api/v1/billing/my/accounts/")
    assert response.status_code == 200
    assert response.data["count"] == 1


@pytest.mark.django_db
def test_staff_cannot_use_my_accounts_endpoint(billing_nurse):
    client = _authed_client(billing_nurse)
    response = client.get("/api/v1/billing/my/accounts/")
    assert response.status_code == 403
