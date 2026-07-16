from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.billing.services import capture_visit_charges
from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic, ClinicStaffMembership
from apps.finance.models import BusinessUnit, FinanceAccessGrant, RevenueEntry, RevenueShareRule
from apps.patients.models import PatientProfile
from apps.users.models import User
from apps.users.tokens import issue_tokens


def _authed_client(user) -> APIClient:
    client = APIClient()
    tokens = issue_tokens(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return client


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def other_doctor(db):
    return User.objects.create_user(email="other@example.com", password="pw", user_type="doctor")


@pytest.fixture
def nurse(db):
    return User.objects.create_user(
        email="nurse@example.com", phone_number="+911234567890", password="pw", user_type="nurse"
    )


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


# ---------------------------------------------------------------------------
# Business units
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_doctor_can_create_business_unit(doctor):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/business-units/", {"name": "My Lab", "unit_type": "lab"}
    )
    assert response.status_code == 201, response.data
    assert BusinessUnit.objects.filter(owner=doctor, name="My Lab").exists()


@pytest.mark.django_db
def test_non_doctor_cannot_create_business_unit(nurse):
    client = _authed_client(nurse)
    response = client.post(
        "/api/v1/finance/business-units/", {"name": "My Lab", "unit_type": "lab"}
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_doctor_cannot_link_a_clinic_they_do_not_own(doctor, other_doctor):
    others_clinic = Clinic.objects.create(name="Not Mine", owner=other_doctor)
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/business-units/",
        {
            "name": "Sneaky",
            "unit_type": "clinic",
            "clinic": str(others_clinic.external_id),
        },
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_doctor_only_sees_own_business_units(doctor, other_doctor):
    BusinessUnit.objects.create(owner=doctor, name="Mine", unit_type="lab")
    BusinessUnit.objects.create(owner=other_doctor, name="Not mine", unit_type="lab")
    client = _authed_client(doctor)
    response = client.get("/api/v1/finance/business-units/")
    assert response.status_code == 200
    names = {row["name"] for row in response.data["results"]}
    assert names == {"Mine"}


# ---------------------------------------------------------------------------
# Revenue entries
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_doctor_can_create_manual_entry(doctor):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/entries/",
        {"source_type": "home_visit", "amount": "500.00", "amount_received": "500.00"},
    )
    assert response.status_code == 201, response.data
    assert response.data["status"] == "received"
    assert response.data["doctor_share_amount"] == "500.00"


@pytest.mark.django_db
def test_non_doctor_cannot_create_manual_entry(nurse):
    client = _authed_client(nurse)
    response = client.post(
        "/api/v1/finance/entries/", {"source_type": "home_visit", "amount": "500.00"}
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_entries_list_includes_both_earned_and_owned(doctor, other_doctor, clinic):
    RevenueShareRule.objects.create(
        clinic=clinic, doctor=other_doctor, doctor_share_percentage=60, enabled=True
    )
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p@example.com", password="pw", user_type="patient")
    )
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=other_doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("1000.00"),
        payment_mode=PaymentMode.CASH,
    )
    capture_visit_charges(visit)

    owner_client = _authed_client(doctor)
    response = owner_client.get("/api/v1/finance/entries/")
    assert response.status_code == 200
    assert response.data["count"] == 1
    # response.data holds pre-render Python objects (DRF only coerces to
    # string once actually rendered to JSON bytes) — SerializerMethodField
    # returns the raw Decimal from the model property untouched.
    assert response.data["results"][0]["your_share_amount"] == Decimal("400.00")

    doctor_client = _authed_client(other_doctor)
    response2 = doctor_client.get("/api/v1/finance/entries/")
    assert response2.data["results"][0]["your_share_amount"] == Decimal("600.00")


@pytest.mark.django_db
def test_billing_originated_entry_cannot_be_patched_via_finance_api(doctor, clinic):
    """Billing/quick-pay is the only visit-side capture path — the
    resulting bridged entry is linked via `charge_item`, not `visit`, but
    must be rejected here exactly the same way."""
    patient = PatientProfile.objects.create(
        user=User.objects.create_user(email="p2@example.com", password="pw", user_type="patient")
    )
    visit = Visit.objects.create(
        patient=patient,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Cough",
        diagnosis="Cold",
        amount_paid=Decimal("500.00"),
        payment_mode=PaymentMode.CASH,
    )
    capture_visit_charges(visit)
    entry = RevenueEntry.objects.get(doctor=doctor)

    client = _authed_client(doctor)
    response = client.patch(
        f"/api/v1/finance/entries/{entry.external_id}/", {"amount_received": "0.00"}
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_doctor_can_settle_a_pending_manual_entry(doctor):
    client = _authed_client(doctor)
    create_response = client.post(
        "/api/v1/finance/entries/", {"source_type": "insurance_claim", "amount": "10000.00"}
    )
    external_id = create_response.data["external_id"]
    patch_response = client.patch(
        f"/api/v1/finance/entries/{external_id}/", {"amount_received": "10000.00"}
    )
    assert patch_response.status_code == 200
    assert patch_response.data["status"] == "received"
    assert patch_response.data["settled_on"] is not None


@pytest.mark.django_db
def test_other_doctor_cannot_edit_someone_elses_entry(doctor, other_doctor):
    client = _authed_client(doctor)
    create_response = client.post(
        "/api/v1/finance/entries/", {"source_type": "home_visit", "amount": "500.00"}
    )
    external_id = create_response.data["external_id"]

    other_client = _authed_client(other_doctor)
    response = other_client.patch(
        f"/api/v1/finance/entries/{external_id}/", {"amount_received": "500.00"}
    )
    assert response.status_code == 404  # not in their doctor/owner queryset


# ---------------------------------------------------------------------------
# Revenue share rules
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_owner_can_create_share_rule_for_active_staff_doctor(doctor, other_doctor, clinic):
    ClinicStaffMembership.objects.create(clinic=clinic, user=other_doctor, role="doctor")
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/share-rules/",
        {
            "clinic": str(clinic.external_id),
            "doctor": str(other_doctor.external_id),
            "doctor_share_percentage": "60.00",
        },
    )
    assert response.status_code == 201, response.data


@pytest.mark.django_db
def test_non_owner_cannot_create_share_rule(doctor, other_doctor, clinic):
    ClinicStaffMembership.objects.create(clinic=clinic, user=other_doctor, role="doctor")
    client = _authed_client(other_doctor)  # not the clinic owner
    response = client.post(
        "/api/v1/finance/share-rules/",
        {
            "clinic": str(clinic.external_id),
            "doctor": str(other_doctor.external_id),
            "doctor_share_percentage": "60.00",
        },
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_owner_cannot_create_share_rule_for_non_staff_doctor(doctor, other_doctor, clinic):
    # other_doctor has never joined this clinic as staff.
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/share-rules/",
        {
            "clinic": str(clinic.external_id),
            "doctor": str(other_doctor.external_id),
            "doctor_share_percentage": "60.00",
        },
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_affected_doctor_can_read_own_rule_but_not_others(doctor, other_doctor, clinic):
    ClinicStaffMembership.objects.create(clinic=clinic, user=other_doctor, role="doctor")
    RevenueShareRule.objects.create(clinic=clinic, doctor=other_doctor, doctor_share_percentage=60)
    client = _authed_client(other_doctor)
    response = client.get(f"/api/v1/finance/share-rules/?clinic={clinic.external_id}")
    assert response.status_code == 200
    assert len(response.data["results"]) == 1


@pytest.mark.django_db
def test_share_rule_clinic_and_doctor_are_immutable_on_patch(doctor, other_doctor, clinic):
    # Ownership is checked against the rule's CURRENT clinic, so letting a
    # PATCH rewrite `clinic` would let an owner move a rule onto a clinic
    # they don't own; `doctor` is equally the rule's identity.
    ClinicStaffMembership.objects.create(clinic=clinic, user=other_doctor, role="doctor")
    rule = RevenueShareRule.objects.create(
        clinic=clinic, doctor=other_doctor, doctor_share_percentage=60
    )
    second_clinic = Clinic.objects.create(name="Second Clinic", owner=doctor)
    client = _authed_client(doctor)

    response = client.patch(
        f"/api/v1/finance/share-rules/{rule.external_id}/",
        {"clinic": str(second_clinic.external_id)},
    )
    assert response.status_code == 400

    response = client.patch(
        f"/api/v1/finance/share-rules/{rule.external_id}/",
        {"doctor": str(doctor.external_id)},
    )
    assert response.status_code == 400

    # Non-identity fields stay editable.
    response = client.patch(
        f"/api/v1/finance/share-rules/{rule.external_id}/",
        {"doctor_share_percentage": "50.00"},
    )
    assert response.status_code == 200, response.data


@pytest.mark.django_db
def test_business_unit_clinic_and_unit_type_are_immutable_on_patch(doctor, clinic):
    unit = BusinessUnit.objects.create(
        owner=doctor, clinic=clinic, name=clinic.name, unit_type="clinic"
    )
    client = _authed_client(doctor)

    response = client.patch(
        f"/api/v1/finance/business-units/{unit.external_id}/", {"unit_type": "pharmacy"}
    )
    assert response.status_code == 400

    second_clinic = Clinic.objects.create(name="Second Clinic", owner=doctor)
    response = client.patch(
        f"/api/v1/finance/business-units/{unit.external_id}/",
        {"clinic": str(second_clinic.external_id)},
    )
    assert response.status_code == 400

    response = client.patch(
        f"/api/v1/finance/business-units/{unit.external_id}/", {"name": "Renamed"}
    )
    assert response.status_code == 200, response.data


# ---------------------------------------------------------------------------
# Finance access grants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_doctor_can_grant_access_by_email(doctor, nurse):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/access-grants/", {"grantee_email": "nurse@example.com"}
    )
    assert response.status_code == 201, response.data
    assert FinanceAccessGrant.objects.filter(doctor=doctor, grantee=nurse).exists()


@pytest.mark.django_db
def test_doctor_can_grant_access_by_phone(doctor, nurse):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/access-grants/", {"grantee_phone_number": "+911234567890"}
    )
    assert response.status_code == 201, response.data


@pytest.mark.django_db
def test_grant_rejects_both_email_and_phone(doctor):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/access-grants/",
        {"grantee_email": "a@example.com", "grantee_phone_number": "+911234567890"},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_grant_rejects_self(doctor):
    client = _authed_client(doctor)
    response = client.post(
        "/api/v1/finance/access-grants/", {"grantee_email": "doctor@example.com"}
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_grantee_can_view_granted_doctor_entries(doctor, nurse):
    RevenueEntry.objects.create(doctor=doctor, source_type="home_visit", amount=Decimal("300.00"))
    client = _authed_client(doctor)
    client.post("/api/v1/finance/access-grants/", {"grantee_email": "nurse@example.com"})

    grantee_client = _authed_client(nurse)
    response = grantee_client.get(f"/api/v1/finance/entries/?doctor={doctor.external_id}")
    assert response.status_code == 200
    assert response.data["count"] == 1


@pytest.mark.django_db
def test_revoked_grant_cuts_access_immediately(doctor, nurse):
    client = _authed_client(doctor)
    grant_response = client.post(
        "/api/v1/finance/access-grants/", {"grantee_email": "nurse@example.com"}
    )
    external_id = grant_response.data["external_id"]
    revoke_response = client.post(f"/api/v1/finance/access-grants/{external_id}/revoke/")
    assert revoke_response.status_code == 200

    grantee_client = _authed_client(nurse)
    response = grantee_client.get(f"/api/v1/finance/entries/?doctor={doctor.external_id}")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_requires_authentication():
    response = APIClient().get("/api/v1/finance/dashboard/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_dashboard_returns_month_to_date_by_default(doctor):
    client = _authed_client(doctor)
    response = client.get("/api/v1/finance/dashboard/")
    assert response.status_code == 200
    assert response.data["period"]["from"].endswith("-01")
    assert response.data["period"]["granularity"] == "day"
    assert response.data["viewer"]["via_grant"] is False


@pytest.mark.django_db
def test_dashboard_rejects_invalid_date(doctor):
    client = _authed_client(doctor)
    response = client.get("/api/v1/finance/dashboard/?from=not-a-date")
    assert response.status_code == 400


@pytest.mark.django_db
def test_dashboard_rejects_from_after_to(doctor):
    client = _authed_client(doctor)
    response = client.get("/api/v1/finance/dashboard/?from=2026-07-15&to=2026-07-01")
    assert response.status_code == 400


@pytest.mark.django_db
def test_dashboard_rejects_invalid_granularity(doctor):
    client = _authed_client(doctor)
    response = client.get(
        "/api/v1/finance/dashboard/?from=2026-07-01&to=2026-07-15&granularity=fortnight"
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_dashboard_auto_granularity_switches_to_month_for_long_spans(doctor):
    client = _authed_client(doctor)
    response = client.get("/api/v1/finance/dashboard/?from=2026-01-01&to=2026-12-31")
    assert response.status_code == 200
    assert response.data["period"]["granularity"] == "month"


@pytest.mark.django_db
def test_dashboard_reflects_real_entries_end_to_end(doctor):
    client = _authed_client(doctor)
    client.post(
        "/api/v1/finance/entries/",
        {"source_type": "home_visit", "amount": "500.00", "amount_received": "500.00"},
    )
    response = client.get("/api/v1/finance/dashboard/")
    assert response.status_code == 200
    assert response.data["totals"]["gross"] == Decimal("500.00")
    assert response.data["by_source"][0]["source_type"] == "home_visit"


@pytest.mark.django_db
def test_non_privileged_grantee_cannot_view_dashboard(doctor, nurse):
    client = _authed_client(nurse)
    response = client.get(f"/api/v1/finance/dashboard/?doctor={doctor.external_id}")
    assert response.status_code == 403
