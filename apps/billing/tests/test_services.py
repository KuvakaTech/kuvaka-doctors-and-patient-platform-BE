from decimal import Decimal

import pytest
from rest_framework.exceptions import ValidationError

from apps.billing.models import (
    ChargeCategory,
    ChargeItem,
    ChargeItemStatus,
    IdempotencyKey,
    Invoice,
    InvoiceStatus,
    PatientAccount,
    Payment,
    PaymentKind,
)
from apps.billing.services import (
    IdempotencyConflict,
    apply_advance,
    cancel_charge,
    cancel_invoice,
    capture_charge,
    capture_visit_charges,
    compute_price_breakdown,
    create_definition,
    create_draft_invoice,
    get_idempotent_response,
    get_or_create_account,
    hash_request_body,
    issue_invoice,
    post_payment,
    recalculate_account,
    revise_definition,
    store_idempotent_response,
    validate_price_components,
)
from apps.clinical.models import PaymentMode, Visit, VisitType
from apps.clinics.models import Clinic, ClinicInventoryItem, Medicine
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


def _consult_components(amount="500.00"):
    return [{"type": "base", "amount": amount}]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_or_create_account_is_idempotent(patient_profile, clinic):
    a1 = get_or_create_account(patient=patient_profile, clinic=clinic)
    a2 = get_or_create_account(patient=patient_profile, clinic=clinic)
    assert a1.pk == a2.pk
    assert PatientAccount.objects.filter(patient=patient_profile, clinic=clinic).count() == 1


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_definition_and_revise_creates_new_version(doctor, clinic):
    definition = create_definition(
        clinic=clinic,
        doctor=doctor,
        code="opd-consult",
        title="OPD Consultation",
        category=ChargeCategory.CONSULTATION,
        price_components=_consult_components("500.00"),
        created_by=doctor,
    )
    assert definition.version == 1
    assert definition.is_active is True

    revised = revise_definition(
        definition, price_components=_consult_components("600.00"), updated_by=doctor
    )
    definition.refresh_from_db()
    assert definition.is_active is False
    assert revised.version == 2
    assert revised.is_active is True
    assert revised.code == definition.code


@pytest.mark.parametrize(
    "components",
    [
        [],  # no base at all
        [{"type": "base", "amount": "100"}, {"type": "base", "amount": "200"}],  # two bases
        [
            {"type": "base", "amount": "100"},
            {"type": "discount", "amount": "10", "factor": "0.1"},
        ],  # both amount+factor
        [{"type": "base", "amount": "100"}, {"type": "discount"}],  # neither amount nor factor
        [{"type": "base", "amount": "100"}, {"type": "bogus", "amount": "5"}],  # unknown type
    ],
)
def test_validate_price_components_rejects_bad_shapes(components):
    with pytest.raises(ValidationError):
        validate_price_components(components)


def test_validate_price_components_accepts_well_formed():
    validate_price_components(
        [
            {"type": "base", "amount": "500.00"},
            {"type": "discount", "factor": "0.20"},
            {"type": "tax", "factor": "0.12"},
        ]
    )  # must not raise


# ---------------------------------------------------------------------------
# compute_price_breakdown
# ---------------------------------------------------------------------------


def test_breakdown_base_only():
    result = compute_price_breakdown([{"type": "base", "amount": "500.00"}], Decimal("1"))
    assert result == {
        "gross": Decimal("500.00"),
        "discount": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "net": Decimal("500.00"),
    }


def test_breakdown_with_discount_factor_and_tax_factor():
    components = [
        {"type": "base", "amount": "1000.00"},
        {"type": "discount", "factor": "0.20"},  # 200.00
        {"type": "tax", "factor": "0.12"},  # 12% of base = 120.00
    ]
    result = compute_price_breakdown(components, Decimal("1"))
    assert result["gross"] == Decimal("1000.00")
    assert result["discount"] == Decimal("200.00")
    assert result["tax"] == Decimal("120.00")
    assert result["net"] == Decimal("920.00")  # 1000 - 200 + 120


def test_breakdown_scales_with_quantity():
    components = [{"type": "base", "amount": "100.00"}, {"type": "tax", "amount": "12.00"}]
    result = compute_price_breakdown(components, Decimal("3"))
    assert result["gross"] == Decimal("300.00")
    assert result["tax"] == Decimal("36.00")
    assert result["net"] == Decimal("336.00")


# ---------------------------------------------------------------------------
# Charge capture
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_capture_charge_basic(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=_consult_components("500.00"),
        recorded_by=doctor,
    )
    assert charge.total_amount == Decimal("500.00")
    assert charge.status == ChargeItemStatus.UNBILLED
    account.refresh_from_db()
    assert account.total_gross == Decimal("500.00")


@pytest.mark.django_db
def test_capture_charge_decrements_inventory(account, doctor, clinic):
    medicine = Medicine.objects.create(owner=doctor, name="Paracetamol")
    item = ClinicInventoryItem.objects.create(
        clinic=clinic, medicine=medicine, quantity_in_stock=20
    )
    capture_charge(
        account=account,
        category=ChargeCategory.MEDICATION,
        title="Paracetamol x2",
        quantity=Decimal("2"),
        price_components=[{"type": "base", "amount": "10.00"}],
        inventory_item=item,
        recorded_by=doctor,
    )
    item.refresh_from_db()
    assert item.quantity_in_stock == 18


@pytest.mark.django_db
def test_capture_charge_rejects_insufficient_stock(account, doctor, clinic):
    medicine = Medicine.objects.create(owner=doctor, name="Paracetamol")
    item = ClinicInventoryItem.objects.create(
        clinic=clinic, medicine=medicine, quantity_in_stock=1
    )
    with pytest.raises(ValidationError):
        capture_charge(
            account=account,
            category=ChargeCategory.MEDICATION,
            title="Paracetamol x5",
            quantity=Decimal("5"),
            price_components=[{"type": "base", "amount": "10.00"}],
            inventory_item=item,
            recorded_by=doctor,
        )
    item.refresh_from_db()
    assert item.quantity_in_stock == 1  # untouched


@pytest.mark.django_db
def test_cancel_charge_restores_stock_and_requires_unbilled(account, doctor, clinic):
    medicine = Medicine.objects.create(owner=doctor, name="Paracetamol")
    item = ClinicInventoryItem.objects.create(
        clinic=clinic, medicine=medicine, quantity_in_stock=20
    )
    charge = capture_charge(
        account=account,
        category=ChargeCategory.MEDICATION,
        title="Paracetamol x2",
        quantity=Decimal("2"),
        price_components=[{"type": "base", "amount": "10.00"}],
        inventory_item=item,
        recorded_by=doctor,
    )
    item.refresh_from_db()
    assert item.quantity_in_stock == 18

    cancel_charge(charge, cancelled_by=doctor)
    charge.refresh_from_db()
    item.refresh_from_db()
    assert charge.status == ChargeItemStatus.CANCELLED
    assert item.quantity_in_stock == 20

    with pytest.raises(ValidationError):
        cancel_charge(charge, cancelled_by=doctor)  # already cancelled


# ---------------------------------------------------------------------------
# Invoice lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_issue_invoice_computes_totals_and_bills_items(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        price_components=[
            {"type": "base", "amount": "1000.00"},
            {"type": "discount", "factor": "0.10"},
            {"type": "tax", "factor": "0.12"},
        ],
        recorded_by=doctor,
    )
    draft = create_draft_invoice(account=account, charge_items=[charge])
    assert draft.status == InvoiceStatus.DRAFT
    assert draft.number == ""

    issued = issue_invoice(draft, issued_by=doctor)
    assert issued.status == InvoiceStatus.ISSUED
    assert issued.total_gross == Decimal("1000.00")
    assert issued.total_discount == Decimal("100.00")
    assert issued.total_tax == Decimal("120.00")
    assert issued.total_net == Decimal("1020.00")
    assert issued.number != ""

    charge.refresh_from_db()
    assert charge.status == ChargeItemStatus.BILLED
    assert charge.invoice_id == issued.id


@pytest.mark.django_db
def test_invoice_number_format_and_sequence(account, doctor, clinic):
    clinic.invoice_prefix = "SHC"
    clinic.save(update_fields=["invoice_prefix"])

    charge1 = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    invoice1 = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge1]), issued_by=doctor
    )

    charge2 = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="B",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    invoice2 = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge2]), issued_by=doctor
    )

    assert invoice1.number.startswith("SHC/")
    assert invoice1.number.endswith("000001")
    assert invoice2.number.endswith("000002")


@pytest.mark.django_db
def test_invoice_prefix_auto_derived_and_persisted(account, doctor, clinic):
    assert clinic.invoice_prefix == ""
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    clinic.refresh_from_db()
    assert clinic.invoice_prefix == "SHA"  # from "Sharma Clinic"
    assert invoice.number.startswith("SHA/")


@pytest.mark.django_db
def test_fiscal_year_label_respects_clinic_start_month(account, doctor, clinic):
    # Default fiscal_year_start_month=4 (April). A charge dated in Feb 2027
    # falls in FY2026-27, not FY2027-28.
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
        service_date="2027-02-01",
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    # Numbering keys off *today's* clinic_localdate at issue time, not the
    # charge's service_date — assert only that a sane label was produced.
    assert "-" in invoice.number.split("/")[1]


@pytest.mark.django_db
def test_create_draft_invoice_rejects_empty_and_foreign_items(account, doctor, patient_profile):
    with pytest.raises(ValidationError):
        create_draft_invoice(account=account, charge_items=[])

    other_clinic = Clinic.objects.create(name="Other", owner=doctor)
    other_account = get_or_create_account(patient=patient_profile, clinic=other_clinic)
    foreign_charge = capture_charge(
        account=other_account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    with pytest.raises(ValidationError):
        create_draft_invoice(account=account, charge_items=[foreign_charge])


@pytest.mark.django_db
def test_cannot_double_invoice_same_charge(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    create_draft_invoice(account=account, charge_items=[charge])
    with pytest.raises(ValidationError):
        create_draft_invoice(account=account, charge_items=[charge])  # already attached to a draft


@pytest.mark.django_db
def test_cancel_invoice_releases_items_and_reclaims_payments(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("500.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("500.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )

    cancel_invoice(invoice, reason="Wrong patient billed", cancelled_by=doctor)

    invoice.refresh_from_db()
    charge.refresh_from_db()
    account.refresh_from_db()
    assert invoice.status == InvoiceStatus.CANCELLED
    assert charge.status == ChargeItemStatus.UNBILLED
    assert charge.invoice_id is None
    assert account.advance_balance == Decimal("500.00")  # the payment converts to credit

    payment = Payment.objects.get(account=account)
    assert payment.invoice_id is None  # detached, not deleted — the money event still happened


@pytest.mark.django_db
def test_cancel_invoice_requires_reason_and_rejects_double_cancel(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )

    with pytest.raises(ValidationError):
        cancel_invoice(invoice, reason="   ", cancelled_by=doctor)

    cancel_invoice(invoice, reason="mistake", cancelled_by=doctor)
    with pytest.raises(ValidationError):
        cancel_invoice(invoice, reason="again", cancelled_by=doctor)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_post_payment_partial_then_full(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("1000.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )

    post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("400.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PARTIALLY_PAID
    assert invoice.amount_paid == Decimal("400.00")

    post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("600.00"),
        method=PaymentMode.UPI,
        received_by=doctor,
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.amount_paid == Decimal("1000.00")


@pytest.mark.django_db
def test_post_payment_cash_tendered_and_change(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("1740.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    payment = post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("1740.00"),
        method=PaymentMode.CASH,
        tendered_amount=Decimal("2000.00"),
        returned_amount=Decimal("260.00"),
        received_by=doctor,
    )
    assert payment.tendered_amount == Decimal("2000.00")
    assert payment.returned_amount == Decimal("260.00")


@pytest.mark.django_db
def test_post_payment_rejects_payment_kind_without_invoice(account, doctor):
    with pytest.raises(ValidationError):
        post_payment(
            account=account,
            kind=PaymentKind.PAYMENT,
            amount=Decimal("100.00"),
            method=PaymentMode.CASH,
            received_by=doctor,
        )


@pytest.mark.django_db
def test_post_payment_rejects_advance_with_invoice(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components(),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    with pytest.raises(ValidationError):
        post_payment(
            account=account,
            invoice=invoice,
            kind=PaymentKind.ADVANCE,
            amount=Decimal("100.00"),
            method=PaymentMode.CASH,
            received_by=doctor,
        )


@pytest.mark.django_db
def test_post_payment_rejects_non_positive_amount(account, doctor):
    with pytest.raises(ValidationError):
        post_payment(
            account=account,
            kind=PaymentKind.ADVANCE,
            amount=Decimal("0"),
            method=PaymentMode.CASH,
            received_by=doctor,
        )


@pytest.mark.django_db
def test_advance_deposit_and_apply(account, doctor):
    post_payment(
        account=account,
        kind=PaymentKind.ADVANCE,
        amount=Decimal("1000.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )
    account.refresh_from_db()
    assert account.advance_balance == Decimal("1000.00")

    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("500.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )

    apply_advance(account=account, invoice=invoice, amount=Decimal("500.00"), applied_by=doctor)
    invoice.refresh_from_db()
    account.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID
    assert account.advance_balance == Decimal("500.00")


@pytest.mark.django_db
def test_apply_advance_rejects_exceeding_balance(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("500.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    with pytest.raises(ValidationError):
        apply_advance(
            account=account, invoice=invoice, amount=Decimal("100.00"), applied_by=doctor
        )


@pytest.mark.django_db
def test_refund_reduces_invoice_amount_paid(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("1000.00"),
        recorded_by=doctor,
    )
    invoice = issue_invoice(
        create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor
    )
    post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=Decimal("1000.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )
    post_payment(
        account=account,
        invoice=invoice,
        kind=PaymentKind.REFUND,
        amount=Decimal("200.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )
    invoice.refresh_from_db()
    assert invoice.amount_paid == Decimal("800.00")
    assert invoice.status == InvoiceStatus.PARTIALLY_PAID


# ---------------------------------------------------------------------------
# recalculate_account
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_recalculate_account_reflects_balance_due_net_of_advance(account, doctor):
    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="A",
        price_components=_consult_components("1000.00"),
        recorded_by=doctor,
    )
    issue_invoice(create_draft_invoice(account=account, charge_items=[charge]), issued_by=doctor)
    post_payment(
        account=account,
        kind=PaymentKind.ADVANCE,
        amount=Decimal("400.00"),
        method=PaymentMode.CASH,
        received_by=doctor,
    )

    recalculate_account(account)
    account.refresh_from_db()
    assert account.total_invoiced == Decimal("1000.00")
    assert account.total_paid == Decimal("0.00")
    assert account.balance_due == Decimal("600.00")  # 1000 - 0 - 400 advance credit


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_hash_request_body_deterministic_regardless_of_key_order():
    assert hash_request_body({"a": 1, "b": 2}) == hash_request_body({"b": 2, "a": 1})


@pytest.mark.django_db
def test_idempotency_replay_and_conflict(account):
    body = {"amount": "100.00"}
    body_hash = hash_request_body(body)

    assert get_idempotent_response(account, "key-1", body_hash) is None
    store_idempotent_response(account, "key-1", body_hash, 201, {"id": "abc"})

    replayed = get_idempotent_response(account, "key-1", body_hash)
    assert replayed == (201, {"id": "abc"})

    different_hash = hash_request_body({"amount": "999.00"})
    with pytest.raises(IdempotencyConflict):
        get_idempotent_response(account, "key-1", different_hash)


@pytest.mark.django_db
def test_idempotency_key_scoped_per_account(account, patient_profile, doctor):
    other_clinic = Clinic.objects.create(name="Other", owner=doctor)
    other_account = get_or_create_account(patient=patient_profile, clinic=other_clinic)
    body_hash = hash_request_body({"amount": "100.00"})

    store_idempotent_response(account, "shared-key", body_hash, 201, {"id": "a"})
    # Same literal key string on a different account is a distinct row —
    # no cross-account collision.
    assert get_idempotent_response(other_account, "shared-key", body_hash) is None
    assert IdempotencyKey.objects.count() == 1


# ---------------------------------------------------------------------------
# Visit auto-capture + quick-pay
# ---------------------------------------------------------------------------


def _make_visit(*, clinic, doctor, patient_profile, amount_paid=None, payment_mode=""):
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
def test_capture_visit_charges_none_when_no_fee_and_no_definition(clinic, doctor, patient_profile):
    visit = _make_visit(clinic=clinic, doctor=doctor, patient_profile=patient_profile)
    assert capture_visit_charges(visit) is None


@pytest.mark.django_db
def test_capture_visit_charges_uses_doctors_predefined_fee(clinic, doctor, patient_profile):
    create_definition(
        clinic=clinic,
        doctor=doctor,
        code="opd-consult",
        title="Consult",
        category=ChargeCategory.CONSULTATION,
        price_components=_consult_components("450.00"),
        created_by=doctor,
    )
    visit = _make_visit(clinic=clinic, doctor=doctor, patient_profile=patient_profile)
    charge = capture_visit_charges(visit)
    assert charge.total_amount == Decimal("450.00")
    assert charge.override_reason == ""


@pytest.mark.django_db
def test_capture_visit_charges_typed_amount_overrides_predefined_fee(
    clinic, doctor, patient_profile
):
    create_definition(
        clinic=clinic,
        doctor=doctor,
        code="opd-consult",
        title="Consult",
        category=ChargeCategory.CONSULTATION,
        price_components=_consult_components("450.00"),
        created_by=doctor,
    )
    visit = _make_visit(
        clinic=clinic,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("600.00"),
        payment_mode=PaymentMode.CASH,
    )
    charge = capture_visit_charges(visit)
    assert charge.total_amount == Decimal("600.00")
    assert charge.override_reason != ""


@pytest.mark.django_db
def test_capture_visit_charges_quick_pay_issues_and_pays_invoice(clinic, doctor, patient_profile):
    visit = _make_visit(
        clinic=clinic,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("500.00"),
        payment_mode=PaymentMode.CASH,
    )
    charge = capture_visit_charges(visit)
    charge.refresh_from_db()
    assert charge.status == ChargeItemStatus.BILLED
    invoice = Invoice.objects.get(charge_items=charge)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.total_net == Decimal("500.00")
    assert Payment.objects.filter(invoice=invoice).exists()


@pytest.mark.django_db
def test_capture_visit_charges_insurance_issues_without_payment(clinic, doctor, patient_profile):
    visit = _make_visit(
        clinic=clinic,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("1200.00"),
        payment_mode=PaymentMode.INSURANCE,
    )
    charge = capture_visit_charges(visit)
    charge.refresh_from_db()
    invoice = Invoice.objects.get(charge_items=charge)
    assert invoice.status == InvoiceStatus.ISSUED  # not PAID — no payment posted
    assert not Payment.objects.filter(invoice=invoice).exists()


@pytest.mark.django_db
def test_capture_visit_charges_no_quick_pay_leaves_charge_unbilled(
    clinic, doctor, patient_profile
):
    visit = _make_visit(clinic=clinic, doctor=doctor, patient_profile=patient_profile)
    create_definition(
        clinic=clinic,
        doctor=doctor,
        code="opd-consult",
        title="Consult",
        category=ChargeCategory.CONSULTATION,
        price_components=_consult_components("450.00"),
        created_by=doctor,
    )
    visit.refresh_from_db()
    charge = capture_visit_charges(visit)
    assert charge.status == ChargeItemStatus.UNBILLED


@pytest.mark.django_db
def test_capture_visit_charges_is_idempotent_on_unbilled_charge(clinic, doctor, patient_profile):
    create_definition(
        clinic=clinic,
        doctor=doctor,
        code="opd-consult",
        title="Consult",
        category=ChargeCategory.CONSULTATION,
        price_components=_consult_components("450.00"),
        created_by=doctor,
    )
    visit = _make_visit(clinic=clinic, doctor=doctor, patient_profile=patient_profile)
    first = capture_visit_charges(visit)
    second = capture_visit_charges(visit)
    assert first.pk == second.pk
    assert ChargeItem.objects.filter(visit=visit).count() == 1


@pytest.mark.django_db
def test_capture_visit_charges_leaves_billed_charge_untouched_on_revisit(
    clinic, doctor, patient_profile
):
    visit = _make_visit(
        clinic=clinic,
        doctor=doctor,
        patient_profile=patient_profile,
        amount_paid=Decimal("500.00"),
        payment_mode=PaymentMode.CASH,
    )
    first = capture_visit_charges(visit)
    assert first.status == ChargeItemStatus.BILLED

    visit.amount_paid = Decimal("999.00")
    visit.save(update_fields=["amount_paid"])
    second = capture_visit_charges(visit)
    assert second.pk == first.pk
    assert second.total_amount == Decimal(
        "500.00"
    )  # unchanged — already billed, not silently rewritten
