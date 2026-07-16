"""
Cross-module integration for the billing <-> finance bridge: the doctor's dashboard
reconciling with billing's own reconciliation day-book, and revenue
splits carrying correctly through billing-originated income — not just
through the plain manual-entry path covered elsewhere.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db.models import Sum

from apps.billing.models import ChargeCategory, PaymentKind
from apps.billing.services import (
    capture_charge,
    capture_visit_charges,
    create_draft_invoice,
    get_or_create_account,
    issue_invoice,
    post_payment,
)
from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic
from apps.finance.models import RevenueShareRule
from apps.finance.services import compute_dashboard
from apps.patients.models import PatientProfile
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def visiting_doctor(db):
    return User.objects.create_user(email="visit@example.com", password="pw", user_type="doctor")


@pytest.fixture
def patient_profile(db):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


def _visit(*, clinic, doctor, patient_profile, amount_paid=None, payment_mode=""):
    return Visit.objects.create(
        patient=patient_profile,
        clinic=clinic,
        doctor=doctor,
        visit_type=VisitType.CONSULTATION,
        chief_complaint="Fever",
        diagnosis="Viral fever",
        amount_paid=amount_paid,
        payment_mode=payment_mode,
    )


@pytest.mark.django_db
def test_two_visits_bridge_to_exactly_two_entries(doctor, patient_profile):
    """Billing/quick-pay is the only visit-side capture path — two
    visits at two clinics for the same doctor must land exactly two
    bridged entries on the dashboard, neither missing nor double-counted."""
    clinic_a = Clinic.objects.create(name="Clinic A", owner=doctor)
    clinic_b = Clinic.objects.create(name="Clinic B", owner=doctor)

    visit_a = _visit(
        clinic=clinic_a,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("400.00"),
        payment_mode=PaymentMode.CASH,
    )
    visit_b = _visit(
        clinic=clinic_b,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("600.00"),
        payment_mode=PaymentMode.CASH,
    )

    for visit in (visit_a, visit_b):
        capture_visit_charges(visit)

    result = compute_dashboard(
        doctor, None, date_from=date.today(), date_to=date.today(), granularity="day"
    )
    assert result["totals"]["gross"] == Decimal("1000.00")
    assert result["totals"]["entry_count"] == 2


@pytest.mark.django_db
def test_split_income_via_visit_quick_pay_reaches_both_dashboards(
    doctor, visiting_doctor, patient_profile
):
    """The canonical revenue-share worked example, run through the actual
    quick-pay hook every clinic now uses."""
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=doctor)
    RevenueShareRule.objects.create(
        clinic=clinic,
        doctor=visiting_doctor,
        doctor_share_percentage=Decimal("60.00"),
        enabled=True,
    )
    visit = _visit(
        clinic=clinic,
        doctor=visiting_doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("1000.00"),
        payment_mode=PaymentMode.UPI,
    )
    capture_visit_charges(visit)

    visiting_dashboard = compute_dashboard(
        visiting_doctor, None, date_from=date.today(), date_to=date.today(), granularity="day"
    )
    owner_dashboard = compute_dashboard(
        doctor, None, date_from=date.today(), date_to=date.today(), granularity="day"
    )
    assert visiting_dashboard["totals"]["gross"] == Decimal("600.00")
    assert owner_dashboard["totals"]["gross"] == Decimal("400.00")


@pytest.mark.django_db
def test_dashboard_received_matches_billing_reconciliation_total(doctor, patient_profile):
    """For an unsplit clinic (doctor == owner), the doctor's own dashboard
    'received' total for a day must equal billing's own day-book total
    for that same day — two different read paths over the same
    underlying money, must never disagree."""
    clinic = Clinic.objects.create(name="Sharma Clinic", owner=doctor)
    account = get_or_create_account(patient=patient_profile, clinic=clinic)

    consult = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=[{"type": "base", "amount": "500.00"}],
        performer=doctor,
        recorded_by=doctor,
    )
    procedure = capture_charge(
        account=account,
        category=ChargeCategory.PROCEDURE,
        title="Dressing",
        price_components=[{"type": "base", "amount": "150.00"}],
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[consult, procedure])
    issue_invoice(invoice, issued_by=doctor)
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("650.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )

    dashboard = compute_dashboard(
        doctor, None, date_from=date.today(), date_to=date.today(), granularity="day"
    )
    day_book_total = account.payments.filter(kind=PaymentKind.PAYMENT).aggregate(
        total=Sum("amount")
    )["total"]

    assert dashboard["totals"]["received"] == day_book_total == Decimal("650.00")
