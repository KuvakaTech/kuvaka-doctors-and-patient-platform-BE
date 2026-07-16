"""
The billing -> finance bridge, exercised through the real
apps.billing.services entry points
(capture_charge/create_draft_invoice/issue_invoice/post_payment/
apply_advance/cancel_invoice) rather than calling
apps.finance.services.record_billing_payment directly — the thing under
test is that billing's own write path correctly drives the bridge end to
end, matching how it will actually run in production.
"""

from decimal import Decimal

import pytest

from apps.billing.models import ChargeCategory, PaymentKind
from apps.billing.services import (
    apply_advance,
    cancel_invoice,
    capture_charge,
    create_draft_invoice,
    get_or_create_account,
    issue_invoice,
    post_payment,
)
from apps.clinics.models import Clinic
from apps.finance.models import EntryStatus, RevenueEntry, RevenueShareRule, RevenueSource
from apps.patients.models import PatientProfile
from apps.users.models import User


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def other_doctor(db):
    return User.objects.create_user(email="other@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


@pytest.fixture
def patient_profile(db):
    user = User.objects.create_user(
        email="patient@example.com", password="pw", user_type="patient"
    )
    return PatientProfile.objects.create(user=user)


@pytest.fixture
def account(patient_profile, clinic):
    return get_or_create_account(patient=patient_profile, clinic=clinic)


def _components(amount):
    return [{"type": "base", "amount": str(amount)}]


def _bridge_entry(charge_item):
    return RevenueEntry.objects.get(charge_item=charge_item)


# ---------------------------------------------------------------------------
# Issue -> PENDING entry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_issue_invoice_creates_one_pending_entry_per_charge_item(account, doctor):
    consult = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    procedure = capture_charge(
        account=account,
        category=ChargeCategory.PROCEDURE,
        title="Dressing",
        price_components=_components("150.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[consult, procedure])
    issue_invoice(invoice, issued_by=doctor)

    consult_entry = _bridge_entry(consult)
    procedure_entry = _bridge_entry(procedure)

    assert consult_entry.source_type == RevenueSource.CLINIC_VISIT
    assert consult_entry.amount == Decimal("500.00")
    assert consult_entry.amount_received == Decimal("0")
    assert consult_entry.status == EntryStatus.PENDING
    assert consult_entry.doctor_id == doctor.id
    assert consult_entry.split_enabled is False

    assert procedure_entry.source_type == RevenueSource.CLINIC_PROCEDURE
    assert procedure_entry.amount == Decimal("150.00")


@pytest.mark.django_db
def test_medication_category_maps_to_pharmacy_sale(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.MEDICATION,
        title="Azithral 500",
        price_components=_components("120.00"),
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    entry = _bridge_entry(charge)
    assert entry.source_type == RevenueSource.PHARMACY_SALE
    # No performer given -> falls back to the clinic owner.
    assert entry.doctor_id == doctor.id


# ---------------------------------------------------------------------------
# Payment -> pro-rata amount_received
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_full_payment_marks_entry_received(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("1000.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("1000.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )
    entry = _bridge_entry(charge)
    assert entry.amount_received == Decimal("1000.00")
    assert entry.status == EntryStatus.RECEIVED
    assert entry.settled_on is not None


@pytest.mark.django_db
def test_partial_payment_distributes_pro_rata_across_items(account, doctor):
    consult = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("300.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    procedure = capture_charge(
        account=account,
        category=ChargeCategory.PROCEDURE,
        title="Dressing",
        price_components=_components("100.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[consult, procedure])
    issue_invoice(invoice, issued_by=doctor)  # total_net = 400.00

    # Pay half (200.00) -> consult (300/400 share) gets 150.00, procedure gets 50.00.
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("200.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )
    consult_entry = _bridge_entry(consult)
    procedure_entry = _bridge_entry(procedure)

    assert consult_entry.amount_received + procedure_entry.amount_received == Decimal("200.00")
    assert consult_entry.status == EntryStatus.PARTIAL
    assert procedure_entry.status == EntryStatus.PARTIAL

    # Pay the remaining balance — both fully received now, and the two
    # payments' allocations must still sum exactly to each item's total.
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("200.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )
    consult_entry.refresh_from_db()
    procedure_entry.refresh_from_db()
    assert consult_entry.amount_received == Decimal("300.00")
    assert consult_entry.status == EntryStatus.RECEIVED
    assert procedure_entry.amount_received == Decimal("100.00")
    assert procedure_entry.status == EntryStatus.RECEIVED


@pytest.mark.django_db
def test_apply_advance_bridges_as_a_paid_event(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    post_payment(
        account=account, kind=PaymentKind.ADVANCE, amount=Decimal("500.00"), received_by=doctor
    )
    apply_advance(account=account, invoice=invoice, amount=Decimal("500.00"), applied_by=doctor)

    entry = _bridge_entry(charge)
    assert entry.amount_received == Decimal("500.00")
    assert entry.status == EntryStatus.RECEIVED


@pytest.mark.django_db
def test_raw_advance_deposit_does_not_bridge(account, doctor):
    """An ADVANCE with no invoice is just a deposit sitting on the account
    — nothing to attribute to a charge/doctor yet."""
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    post_payment(
        account=account, kind=PaymentKind.ADVANCE, amount=Decimal("200.00"), received_by=doctor
    )
    entry = _bridge_entry(charge)
    assert entry.amount_received == Decimal("0")
    assert entry.status == EntryStatus.PENDING


# ---------------------------------------------------------------------------
# Refund -> REFUNDED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_refund_marks_bridged_entries_refunded(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("500.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )
    post_payment(
        account=account,
        kind=PaymentKind.REFUND,
        amount=Decimal("500.00"),
        invoice=invoice,
        method="cash",
        received_by=doctor,
    )
    entry = _bridge_entry(charge)
    assert entry.status == EntryStatus.REFUNDED
    # The historical fact of what was collected before the refund is kept,
    # not zeroed out.
    assert entry.amount_received == Decimal("500.00")


# ---------------------------------------------------------------------------
# Cancel -> CANCELLED, re-issue -> fresh PENDING
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cancel_invoice_marks_entries_cancelled(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    cancel_invoice(invoice, reason="wrong patient", cancelled_by=doctor)

    entry = _bridge_entry(charge)
    assert entry.status == EntryStatus.CANCELLED


@pytest.mark.django_db
def test_reissuing_a_cancelled_charge_refreshes_the_same_entry(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("500.00"),
        performer=doctor,
        recorded_by=doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=doctor)
    cancel_invoice(invoice, reason="wrong amount", cancelled_by=doctor)

    entry_id = _bridge_entry(charge).pk

    charge.refresh_from_db()
    new_invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(new_invoice, issued_by=doctor)

    refreshed = _bridge_entry(charge)
    # Same row, refreshed — not a second bridge entry for the same charge item.
    assert refreshed.pk == entry_id
    assert refreshed.status == EntryStatus.PENDING
    assert refreshed.amount_received == Decimal("0")
    assert RevenueEntry.objects.filter(charge_item=charge).count() == 1


# ---------------------------------------------------------------------------
# Revenue sharing through billing-originated income
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_split_share_rule_applies_through_the_bridge(clinic, doctor, other_doctor, account):
    """Dr other_doctor (visiting) consults at Dr doctor's clinic; a 60%
    share rule is configured — the canonical worked example, but through
    billing capture instead of a plain visit."""
    RevenueShareRule.objects.create(
        clinic=clinic, doctor=other_doctor, doctor_share_percentage=Decimal("60.00"), enabled=True
    )
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_components("1000.00"),
        performer=other_doctor,
        recorded_by=other_doctor,
    )
    invoice = create_draft_invoice(account=account, charge_items=[charge])
    issue_invoice(invoice, issued_by=other_doctor)
    post_payment(
        account=account,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("1000.00"),
        invoice=invoice,
        method="upi",
        received_by=other_doctor,
    )

    entry = _bridge_entry(charge)
    assert entry.split_enabled is True
    assert entry.doctor_id == other_doctor.id
    assert entry.owner_id == doctor.id
    assert entry.doctor_share_amount == Decimal("600.00")
    assert entry.owner_share_amount == Decimal("400.00")
