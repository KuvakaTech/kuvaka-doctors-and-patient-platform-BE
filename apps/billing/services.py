"""
Clinic/hospital billing — the single write path for every account, charge,
invoice, and payment mutation. Explicit service functions called from
views and from the visit-capture hook (once a clinic has cut over) — no signals, matching the whole
codebase's house style.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.billing.models import (
    AccountStatus,
    ChargeCategory,
    ChargeItem,
    ChargeItemDefinition,
    ChargeItemStatus,
    IdempotencyKey,
    Invoice,
    InvoiceNumberCounter,
    InvoiceStatus,
    PatientAccount,
    Payment,
    PaymentKind,
    PriceComponentType,
)
from apps.clinics.models import ClinicInventoryItem
from apps.core.money import DEFAULT_CURRENCY, PaymentMode, clinic_localdate, quantize2
from apps.core.services.financial_audit import FinancialEvent, log_financial_event


class IdempotencyConflict(APIException):
    """A retried request reused an Idempotency-Key with a different body — a
    client bug worth surfacing loudly."""

    status_code = 422
    default_detail = "This Idempotency-Key was already used for a different request."
    default_code = "idempotency_conflict"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def get_or_create_account(*, patient, clinic) -> PatientAccount:
    """
    Lazy-created on first use — a clinical registration that never bills
    gets no account.
    """
    account, _created = PatientAccount.objects.get_or_create(
        patient=patient, clinic=clinic, defaults={"status": AccountStatus.ACTIVE}
    )
    return account


def recalculate_account(account: PatientAccount) -> PatientAccount:
    """
    Recompute total_gross/total_invoiced/total_paid/balance_due from
    source rows. `advance_balance` is the one exception: unlike the
    others, it has no ambiguity-free formula to re-derive from raw
    Payment rows (there is no recorded link between an advance payment
    and which later invoice(s) drew it down), so it is maintained as an
    incrementally-updated running balance instead — every mutation that
    touches it (post_payment(ADVANCE), apply_advance, cancel_invoice) does
    so in the same transaction as this recompute, so it never drifts.
    """
    gross = ChargeItem.objects.filter(
        account=account, status__in=(ChargeItemStatus.UNBILLED, ChargeItemStatus.BILLED)
    ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
    invoice_agg = Invoice.objects.filter(
        account=account,
        status__in=(InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID),
    ).aggregate(net=Sum("total_net"), paid=Sum("amount_paid"))
    total_invoiced = invoice_agg["net"] or Decimal("0")
    total_paid = invoice_agg["paid"] or Decimal("0")

    account.total_gross = quantize2(gross)
    account.total_invoiced = quantize2(total_invoiced)
    account.total_paid = quantize2(total_paid)
    account.balance_due = max(
        Decimal("0"), quantize2(total_invoiced - total_paid - account.advance_balance)
    )
    account.calculated_at = timezone.now()
    account.save(
        update_fields=[
            "total_gross",
            "total_invoiced",
            "total_paid",
            "balance_due",
            "calculated_at",
        ]
    )
    return account


# ---------------------------------------------------------------------------
# Price book
# ---------------------------------------------------------------------------


def validate_price_components(components: list[dict]) -> None:
    bases = [c for c in components if c.get("type") == PriceComponentType.BASE]
    if len(bases) != 1:
        raise ValidationError({"price_components": "Exactly one base component is required."})
    for c in components:
        if c.get("type") not in PriceComponentType.values:
            raise ValidationError(
                {"price_components": f"Unknown component type: {c.get('type')!r}."}
            )
        has_amount = c.get("amount") is not None
        has_factor = c.get("factor") is not None
        if c["type"] == PriceComponentType.BASE:
            if not has_amount or has_factor:
                raise ValidationError(
                    {"price_components": "The base component needs an amount, not a factor."}
                )
        elif has_amount == has_factor:
            raise ValidationError(
                {"price_components": "Each non-base component needs exactly one of amount/factor."}
            )


def compute_price_breakdown(components: list[dict], quantity: Decimal) -> dict:
    """
    gross/discount/tax/net for `quantity` units of a price-component list.
    Each component is computed and quantized to 2dp individually before
    being combined — never re-derived from an already-rounded total — so
    the math is deterministic to the paisa regardless of component order.
    """
    base_per_unit = Decimal("0")
    for c in components:
        if c["type"] == PriceComponentType.BASE:
            base_per_unit += quantize2(Decimal(str(c["amount"])))

    discount_per_unit = Decimal("0")
    tax_per_unit = Decimal("0")
    for c in components:
        if c["type"] == PriceComponentType.BASE:
            continue
        if c.get("amount") is not None:
            delta = quantize2(Decimal(str(c["amount"])))
        else:
            delta = quantize2(base_per_unit * Decimal(str(c["factor"])))
        if c["type"] == PriceComponentType.DISCOUNT:
            discount_per_unit += delta
        else:  # TAX, SURCHARGE
            tax_per_unit += delta

    gross = quantize2(base_per_unit * quantity)
    discount = quantize2(discount_per_unit * quantity)
    tax = quantize2(tax_per_unit * quantity)
    net = quantize2(gross - discount + tax)
    return {"gross": gross, "discount": discount, "tax": tax, "net": net}


def compute_total_amount(components: list[dict], quantity: Decimal) -> Decimal:
    return compute_price_breakdown(components, quantity)["net"]


def create_definition(
    *, clinic, doctor=None, code, title, category, price_components, medicine=None, created_by
) -> ChargeItemDefinition:
    validate_price_components(price_components)
    return ChargeItemDefinition.objects.create(
        clinic=clinic,
        doctor=doctor,
        code=code,
        title=title,
        category=category,
        price_components=price_components,
        medicine=medicine,
        currency=DEFAULT_CURRENCY,
        created_by=created_by,
    )


@transaction.atomic
def revise_definition(
    definition: ChargeItemDefinition, *, price_components, updated_by
) -> ChargeItemDefinition:
    """
    A price change creates version N+1 and deactivates version N — never
    edits price_components in place, so charge items already captured
    from this definition keep their own snapshot untouched.
    """
    validate_price_components(price_components)
    definition.is_active = False
    definition.save(update_fields=["is_active"])
    new_version = ChargeItemDefinition.objects.create(
        clinic=definition.clinic,
        doctor=definition.doctor,
        code=definition.code,
        title=definition.title,
        category=definition.category,
        price_components=price_components,
        medicine=definition.medicine,
        currency=definition.currency,
        version=definition.version + 1,
        created_by=updated_by,
    )
    return new_version


def update_definition_metadata(definition: ChargeItemDefinition, **fields) -> ChargeItemDefinition:
    """Non-price edits (title, medicine link) — applied in place, no new version."""
    for field, value in fields.items():
        setattr(definition, field, value)
    definition.save(update_fields=list(fields))
    return definition


# ---------------------------------------------------------------------------
# Charge capture
# ---------------------------------------------------------------------------


@transaction.atomic
def capture_charge(
    *,
    account: PatientAccount,
    category: str,
    title: str,
    quantity: Decimal = Decimal("1"),
    definition: ChargeItemDefinition | None = None,
    price_components: list[dict] | None = None,
    override_reason: str = "",
    visit=None,
    prescription=None,
    inventory_item: ClinicInventoryItem | None = None,
    performer=None,
    recorded_by,
    service_date: date | None = None,
    notes: str = "",
    request=None,
) -> ChargeItem:
    """
    The single write path for capturing one billable event. `price_components`
    overrides `definition`'s own pricing when given (an ad-hoc/manual
    price) — override_reason is expected whenever the two diverge (not
    DB-enforced; the caller/serializer is responsible for requiring it).
    MEDICATION items decrement clinic stock atomically via F() (same
    pattern as apps.clinics.views.PurchaseOrderReceiveView).
    """
    components = (
        price_components
        if price_components is not None
        else (definition.price_components if definition is not None else [])
    )
    if not components:
        raise ValidationError({"price_components": "Required when no definition is given."})
    validate_price_components(components)
    total_amount = compute_total_amount(components, quantity)
    currency = definition.currency if definition is not None else DEFAULT_CURRENCY

    if inventory_item is not None:
        if inventory_item.quantity_in_stock < quantity:
            raise ValidationError({"inventory_item": "Not enough stock."})
        ClinicInventoryItem.objects.filter(pk=inventory_item.pk).update(
            quantity_in_stock=F("quantity_in_stock") - quantity
        )

    charge = ChargeItem.objects.create(
        account=account,
        patient=account.patient,
        clinic=account.clinic,
        definition=definition,
        title=title,
        category=category,
        quantity=quantity,
        unit_price_components=components,
        total_amount=total_amount,
        currency=currency,
        override_reason=override_reason,
        visit=visit,
        prescription=prescription,
        inventory_item=inventory_item,
        performer=performer,
        service_date=service_date or clinic_localdate(account.clinic),
        recorded_by=recorded_by,
        notes=notes,
    )
    recalculate_account(account)
    log_financial_event(
        request,
        FinancialEvent.CHARGE_CAPTURED,
        actor=recorded_by,
        object_type="charge_item",
        object_id=str(charge.external_id),
        clinic=account.clinic,
        amount=total_amount,
        metadata={"category": category},
    )
    return charge


@transaction.atomic
def cancel_charge(charge: ChargeItem, *, cancelled_by, request=None) -> ChargeItem:
    if charge.status != ChargeItemStatus.UNBILLED:
        raise ValidationError({"status": "Only an unbilled charge can be cancelled."})
    if charge.inventory_item_id is not None:
        ClinicInventoryItem.objects.filter(pk=charge.inventory_item_id).update(
            quantity_in_stock=F("quantity_in_stock") + charge.quantity
        )
    charge.status = ChargeItemStatus.CANCELLED
    charge.save(update_fields=["status"])
    recalculate_account(charge.account)
    log_financial_event(
        request,
        FinancialEvent.CHARGE_CANCELLED,
        actor=cancelled_by,
        object_type="charge_item",
        object_id=str(charge.external_id),
        clinic=charge.clinic,
    )
    return charge


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def _fiscal_year_label(clinic, on_date: date) -> str:
    start_month = clinic.fiscal_year_start_month
    start_year = on_date.year if on_date.month >= start_month else on_date.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def _derive_invoice_prefix(name: str) -> str:
    letters = "".join(ch for ch in name.upper() if ch.isalnum())
    return letters[:3] or "CLN"


def _next_invoice_number(clinic) -> str:
    """
    Assigned under a per-(clinic, fiscal-year) row lock so concurrent
    issues never collide on the same sequence number. Must be called
    inside an existing transaction.atomic block (issue_invoice provides
    one) — select_for_update requires an open transaction.
    """
    today = clinic_localdate(clinic)
    label = _fiscal_year_label(clinic, today)
    InvoiceNumberCounter.objects.get_or_create(
        clinic=clinic, fiscal_year_label=label, defaults={"last_sequence": 0}
    )
    counter = InvoiceNumberCounter.objects.select_for_update().get(
        clinic=clinic, fiscal_year_label=label
    )
    counter.last_sequence += 1
    counter.save(update_fields=["last_sequence"])

    prefix = clinic.invoice_prefix
    if not prefix:
        prefix = _derive_invoice_prefix(clinic.name)
        clinic.invoice_prefix = prefix
        clinic.save(update_fields=["invoice_prefix"])

    return f"{prefix}/{label}/{counter.last_sequence:06d}"


@transaction.atomic
def create_draft_invoice(*, account: PatientAccount, charge_items: list[ChargeItem]) -> Invoice:
    if not charge_items:
        raise ValidationError({"charge_items": "At least one charge item is required."})

    # Re-fetch fresh state rather than trusting the passed-in instances'
    # in-memory fields — a charge item may have been attached to another
    # invoice via a bulk .update() elsewhere (this function's own previous
    # call, for one), which never refreshes any Python object holding it.
    fresh_items = list(ChargeItem.objects.filter(pk__in=[item.pk for item in charge_items]))
    for item in fresh_items:
        if item.account_id != account.id:
            raise ValidationError({"charge_items": "All items must belong to this account."})
        if item.status != ChargeItemStatus.UNBILLED or item.invoice_id is not None:
            raise ValidationError({"charge_items": f"'{item.title}' is not available to invoice."})

    # All charge items on one invoice share a clinic, hence one currency
    # — take it from the items actually being invoiced, not an
    # unrelated definition; every item already carries its own currency
    # from capture time.
    invoice = Invoice.objects.create(
        account=account,
        patient=account.patient,
        clinic=account.clinic,
        currency=fresh_items[0].currency,
    )
    ChargeItem.objects.filter(pk__in=[item.pk for item in fresh_items]).update(invoice=invoice)
    return invoice


@transaction.atomic
def issue_invoice(invoice: Invoice, *, issued_by, request=None) -> Invoice:
    if invoice.status != InvoiceStatus.DRAFT:
        raise ValidationError({"status": "Only a draft invoice can be issued."})
    items = list(invoice.charge_items.filter(status=ChargeItemStatus.UNBILLED))
    if not items:
        raise ValidationError({"charge_items": "Invoice has no charge items."})

    gross = discount = tax = net = Decimal("0")
    snapshot = []
    for item in items:
        breakdown = compute_price_breakdown(item.unit_price_components, item.quantity)
        gross += breakdown["gross"]
        discount += breakdown["discount"]
        tax += breakdown["tax"]
        net += item.total_amount  # the authoritative, already-stored total
        snapshot.append(
            {
                "external_id": str(item.external_id),
                "title": item.title,
                "category": item.category,
                "quantity": str(item.quantity),
                "unit_price_components": item.unit_price_components,
                "total_amount": str(item.total_amount),
            }
        )

    invoice.number = _next_invoice_number(invoice.clinic)
    invoice.line_items_snapshot = snapshot
    invoice.total_gross = quantize2(gross)
    invoice.total_discount = quantize2(discount)
    invoice.total_tax = quantize2(tax)
    invoice.total_net = quantize2(net)
    invoice.status = InvoiceStatus.ISSUED
    invoice.issued_at = timezone.now()
    invoice.issued_by = issued_by
    invoice.save(
        update_fields=[
            "number",
            "line_items_snapshot",
            "total_gross",
            "total_discount",
            "total_tax",
            "total_net",
            "status",
            "issued_at",
            "issued_by",
        ]
    )
    ChargeItem.objects.filter(pk__in=[item.pk for item in items]).update(
        status=ChargeItemStatus.BILLED
    )
    recalculate_account(invoice.account)
    log_financial_event(
        request,
        FinancialEvent.INVOICE_ISSUED,
        actor=issued_by,
        object_type="invoice",
        object_id=str(invoice.external_id),
        clinic=invoice.clinic,
        amount=invoice.total_net,
    )

    from apps.finance.services import record_billing_payment

    record_billing_payment(invoice, event="issued", charge_items=items, request=request)
    return invoice


@transaction.atomic
def cancel_invoice(invoice: Invoice, *, reason: str, cancelled_by, request=None) -> Invoice:
    if invoice.status == InvoiceStatus.CANCELLED:
        raise ValidationError({"status": "Invoice is already cancelled."})
    if not reason.strip():
        raise ValidationError({"reason": "Required to cancel an invoice."})

    # Payments already taken convert to account credit first (a
    # service-level guard, not a caller precondition). Detaching the payment and
    # crediting the account keeps the payment event itself in the ledger
    # (money really was received) while releasing it to be reapplied.
    account = invoice.account
    reclaimed = Payment.objects.filter(invoice=invoice, kind=PaymentKind.PAYMENT).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")
    if reclaimed:
        Payment.objects.filter(invoice=invoice, kind=PaymentKind.PAYMENT).update(invoice=None)
        account.advance_balance = quantize2(account.advance_balance + reclaimed)
        account.save(update_fields=["advance_balance"])

    # Captured before the bulk .update() below clears their `invoice` FK —
    # the bridge needs these items' identities to find their ledger
    # entries, and `invoice.charge_items` would return nothing afterward.
    items = list(invoice.charge_items.all())
    invoice.charge_items.update(invoice=None, status=ChargeItemStatus.UNBILLED)
    invoice.status = InvoiceStatus.CANCELLED
    invoice.cancelled_reason = reason.strip()
    invoice.save(update_fields=["status", "cancelled_reason"])
    recalculate_account(account)
    log_financial_event(
        request,
        FinancialEvent.INVOICE_CANCELLED,
        actor=cancelled_by,
        object_type="invoice",
        object_id=str(invoice.external_id),
        clinic=invoice.clinic,
        metadata={"reason": reason.strip(), "reclaimed_to_advance": str(reclaimed)},
    )

    from apps.finance.services import record_billing_payment

    record_billing_payment(invoice, event="cancelled", charge_items=items, request=request)
    return invoice


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def _derive_invoice_status(invoice: Invoice) -> str:
    if invoice.amount_paid <= 0:
        return InvoiceStatus.ISSUED
    if invoice.amount_paid >= invoice.total_net:
        return InvoiceStatus.PAID
    return InvoiceStatus.PARTIALLY_PAID


@transaction.atomic
def post_payment(
    *,
    account: PatientAccount,
    kind: str,
    amount: Decimal,
    invoice: Invoice | None = None,
    method: str = "",
    tendered_amount: Decimal | None = None,
    returned_amount: Decimal | None = None,
    reference_number: str = "",
    received_by=None,
    notes: str = "",
    request=None,
) -> Payment:
    if amount <= 0:
        raise ValidationError({"amount": "Must be positive."})
    if kind == PaymentKind.PAYMENT and invoice is None:
        raise ValidationError({"invoice": "Required for a payment."})
    if kind == PaymentKind.ADVANCE and invoice is not None:
        raise ValidationError({"invoice": "An advance is account-level, not tied to an invoice."})
    if invoice is not None and invoice.account_id != account.id:
        raise ValidationError({"invoice": "Does not belong to this account."})

    payment = Payment.objects.create(
        account=account,
        invoice=invoice,
        kind=kind,
        amount=quantize2(amount),
        currency=DEFAULT_CURRENCY,
        method=method,
        tendered_amount=tendered_amount,
        returned_amount=returned_amount,
        reference_number=reference_number,
        received_by=received_by,
        notes=notes,
    )

    if kind == PaymentKind.PAYMENT:
        invoice.amount_paid = quantize2(invoice.amount_paid + amount)
        invoice.status = _derive_invoice_status(invoice)
        invoice.save(update_fields=["amount_paid", "status"])
    elif kind == PaymentKind.ADVANCE:
        account.advance_balance = quantize2(account.advance_balance + amount)
        account.save(update_fields=["advance_balance"])
    elif kind == PaymentKind.REFUND:
        if invoice is not None:
            invoice.amount_paid = max(Decimal("0"), quantize2(invoice.amount_paid - amount))
            invoice.status = _derive_invoice_status(invoice)
            invoice.save(update_fields=["amount_paid", "status"])
        else:
            account.advance_balance = max(
                Decimal("0"), quantize2(account.advance_balance - amount)
            )
            account.save(update_fields=["advance_balance"])

    recalculate_account(account)
    log_financial_event(
        request,
        FinancialEvent.REFUND_POSTED
        if kind == PaymentKind.REFUND
        else (
            FinancialEvent.ADVANCE_POSTED
            if kind == PaymentKind.ADVANCE
            else FinancialEvent.PAYMENT_POSTED
        ),
        actor=received_by,
        object_type="payment",
        object_id=str(payment.external_id),
        clinic=account.clinic,
        amount=amount,
    )

    # An account-level ADVANCE deposit has no invoice yet — nothing to
    # bridge until it's actually applied to a bill (apply_advance, below,
    # posts a PAYMENT-kind row against an invoice and bridges that).
    if invoice is not None and kind in (PaymentKind.PAYMENT, PaymentKind.REFUND):
        from apps.finance.services import record_billing_payment

        record_billing_payment(
            invoice,
            event="paid" if kind == PaymentKind.PAYMENT else "refunded",
            payment=payment,
            request=request,
        )
    return payment


@transaction.atomic
def apply_advance(
    *, account: PatientAccount, invoice: Invoice, amount: Decimal, applied_by=None, request=None
) -> Payment:
    if invoice.account_id != account.id:
        raise ValidationError({"invoice": "Does not belong to this account."})
    if invoice.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
        raise ValidationError(
            {"invoice": "Only an issued or partially-paid invoice can receive an advance."}
        )
    if amount <= 0 or amount > account.advance_balance:
        raise ValidationError({"amount": "Exceeds the account's available advance balance."})

    account.advance_balance = quantize2(account.advance_balance - amount)
    account.save(update_fields=["advance_balance"])

    payment = Payment.objects.create(
        account=account,
        invoice=invoice,
        kind=PaymentKind.PAYMENT,
        amount=quantize2(amount),
        currency=DEFAULT_CURRENCY,
        method="",  # drawn from a pre-existing deposit — no new tender event to name
        received_by=applied_by,
        notes="Applied from account advance balance.",
    )
    invoice.amount_paid = quantize2(invoice.amount_paid + amount)
    invoice.status = _derive_invoice_status(invoice)
    invoice.save(update_fields=["amount_paid", "status"])
    recalculate_account(account)
    log_financial_event(
        request,
        FinancialEvent.ADVANCE_APPLIED,
        actor=applied_by,
        object_type="payment",
        object_id=str(payment.external_id),
        clinic=account.clinic,
        amount=amount,
    )

    from apps.finance.services import record_billing_payment

    record_billing_payment(invoice, event="paid", payment=payment, request=request)
    return payment


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def hash_request_body(data: dict) -> str:
    normalized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode()).hexdigest()


def get_idempotent_response(account: PatientAccount, key: str, request_hash: str):
    """
    Returns (status, body) if `key` was already used with this exact body
    (replay); raises IdempotencyConflict if reused with a different body;
    returns None for a genuinely new key.
    """
    existing = IdempotencyKey.objects.filter(account=account, key=key).first()
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise IdempotencyConflict()
    return existing.response_status, existing.response_body


def store_idempotent_response(
    account: PatientAccount, key: str, request_hash: str, status_code: int, body
) -> None:
    IdempotencyKey.objects.create(
        account=account,
        key=key,
        request_hash=request_hash,
        response_status=status_code,
        response_body=body,
    )


# ---------------------------------------------------------------------------
# Visit auto-capture + quick-pay — the only visit-side
# capture path; the pre-cutover
# finance-only path (finance.services.record_visit_revenue) was retired
# once billing became universal.
# ---------------------------------------------------------------------------


@transaction.atomic
def capture_visit_charges(visit, *, request=None) -> ChargeItem | None:
    """
    Auto-captures the visit's CONSULTATION charge from the doctor's own
    predefined fee (falling back to the clinic-wide default), or the
    amount typed on the visit form if one was given (recorded with an
    override_reason either way). When the visit carries payment
    fields, quick-pays it in the same transaction: issues the invoice and
    posts the payment so a solo doctor never opens a billing screen.
    Idempotent per visit — an existing UNBILLED charge is updated in
    place; a BILLED one is left alone (billing history doesn't
    silently rewrite from a later visit edit — correct that through the
    billing screens instead).
    """
    existing = ChargeItem.objects.filter(visit=visit).first()
    if existing is not None:
        if existing.status != ChargeItemStatus.UNBILLED:
            return existing
        return _update_visit_charge(existing, visit)
    return _create_visit_charge(visit, request=request)


def _resolve_consultation_definition(clinic, doctor) -> ChargeItemDefinition | None:
    own = ChargeItemDefinition.objects.filter(
        clinic=clinic,
        doctor=doctor,
        category=ChargeCategory.CONSULTATION,
        is_active=True,
        deleted=False,
    ).first()
    if own is not None:
        return own
    return ChargeItemDefinition.objects.filter(
        clinic=clinic,
        doctor__isnull=True,
        category=ChargeCategory.CONSULTATION,
        is_active=True,
        deleted=False,
    ).first()


def _visit_charge_components(visit, definition):
    if visit.amount_paid:
        override_reason = (
            "Amount entered directly on the visit form."
            if definition is not None
            else "No predefined consultation fee for this doctor/clinic."
        )
        return [
            {"type": PriceComponentType.BASE, "amount": str(visit.amount_paid)}
        ], override_reason
    if definition is not None:
        return definition.price_components, ""
    return None, ""


def _create_visit_charge(visit, *, request=None) -> ChargeItem | None:
    account = get_or_create_account(patient=visit.patient, clinic=visit.clinic)
    definition = _resolve_consultation_definition(visit.clinic, visit.doctor)
    components, override_reason = _visit_charge_components(visit, definition)
    if components is None:
        return None  # nothing to capture — no typed amount, no predefined fee either

    charge = capture_charge(
        account=account,
        category=ChargeCategory.CONSULTATION,
        title="Consultation",
        definition=definition if not override_reason else None,
        price_components=components,
        override_reason=override_reason,
        visit=visit,
        performer=visit.doctor,
        recorded_by=visit.doctor,
        service_date=visit.visit_date,
        request=request,
    )

    if visit.amount_paid and visit.payment_mode:
        _quick_pay(charge, visit, request=request)
        # issue_invoice/post_payment mutate this row via bulk .update()
        # calls under the hood, which never touch this Python instance's
        # in-memory fields — refresh so the caller sees the real status
        # (BILLED, not the stale UNBILLED it was created with).
        charge.refresh_from_db()
    return charge


def _update_visit_charge(charge: ChargeItem, visit) -> ChargeItem:
    definition = charge.definition
    components, override_reason = _visit_charge_components(visit, definition)
    if components is None:
        cancel_charge(charge, cancelled_by=visit.doctor)
        return charge
    charge.unit_price_components = components
    charge.override_reason = override_reason
    charge.total_amount = compute_total_amount(components, charge.quantity)
    charge.service_date = visit.visit_date
    charge.save(
        update_fields=["unit_price_components", "override_reason", "total_amount", "service_date"]
    )
    recalculate_account(charge.account)
    return charge


def _quick_pay(charge: ChargeItem, visit, *, request=None) -> Invoice:
    invoice = create_draft_invoice(account=charge.account, charge_items=[charge])
    invoice = issue_invoice(invoice, issued_by=visit.doctor, request=request)
    if visit.payment_mode != PaymentMode.INSURANCE:
        post_payment(
            account=charge.account,
            invoice=invoice,
            kind=PaymentKind.PAYMENT,
            amount=invoice.total_net,
            method=visit.payment_mode,
            received_by=visit.doctor,
            request=request,
        )
    # Insurance: invoice stays ISSUED/unpaid — no Payment row; the
    # bridged finance entry settles later when real money arrives.
    return invoice
