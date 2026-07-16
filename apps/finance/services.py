"""
Cross-cutting finance operations — the single write path for every
RevenueEntry mutation. Explicit service functions called from views
(visit-capture hooks) and serializers (manual entries), matching the
house style set by apps.patients.services / apps.clinics.permissions —
no signals.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Round
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.money import DEFAULT_CURRENCY, clinic_localdate, quantize2
from apps.core.services.financial_audit import FinancialEvent, log_financial_event
from apps.finance.models import (
    BusinessUnit,
    EntryDirection,
    EntryStatus,
    RevenueEntry,
    RevenueShareRule,
    RevenueSource,
)
from apps.finance.permissions import scope_queryset_to_grant


def derive_entry_status(amount: Decimal, amount_received: Decimal) -> str:
    """
    The only place entry status is computed from money. Never trust a
    status value supplied by a client — the two terminal states
    (REFUNDED/CANCELLED) are the sole exception, set explicitly by the
    caller rather than derived here.
    """
    # A zero-amount entry (an MR engagement with only in-kind items —
    # amount=0 is explicitly valid) has nothing outstanding by definition:
    # 0 received of 0 owed is fully accounted for, not "pending forever".
    # Must be checked before the amount_received<=0 branch below, which
    # would otherwise catch amount=0/received=0 first and misclassify it.
    if amount <= 0:
        return EntryStatus.RECEIVED
    if amount_received <= 0:
        return EntryStatus.PENDING
    if amount_received >= amount:
        return EntryStatus.RECEIVED
    return EntryStatus.PARTIAL


def resolve_entry_attribution(*, business_unit: BusinessUnit | None, clinic=None):
    """
    Derive the entry's `clinic` — copied from `business_unit.clinic` when
    the unit has one; an explicit `clinic` that disagrees is a validation
    error. `clinic` is
    never independently writable beyond this single choke point.
    """
    if business_unit is not None and business_unit.clinic_id is not None:
        if clinic is not None and clinic.pk != business_unit.clinic_id:
            raise ValidationError({"clinic": "Does not match business_unit's clinic."})
        return business_unit.clinic
    return clinic


def get_active_share_rule(clinic, doctor) -> RevenueShareRule | None:
    """
    The enabled RevenueShareRule for (clinic, doctor), or None if no split
    applies — either because none is configured, it's disabled, or the
    "conducting doctor" is the clinic owner themselves (splitting with
    yourself is a no-op).
    """
    if clinic is None or clinic.owner_id == doctor.id:
        return None
    return RevenueShareRule.objects.filter(
        clinic=clinic, doctor=doctor, enabled=True, deleted=False
    ).first()


def _clinic_business_unit(clinic) -> BusinessUnit | None:
    """The clinic owner's own BusinessUnit for this clinic, if one exists."""
    if clinic is None:
        return None
    return BusinessUnit.objects.filter(
        clinic=clinic, owner_id=clinic.owner_id, deleted=False
    ).first()


# ---------------------------------------------------------------------------
# The billing bridge
# ---------------------------------------------------------------------------

# Keyed on the plain string values of apps.billing.models.ChargeCategory
# (never imported here — apps.finance never imports apps.billing at module
# level). A category with no explicit mapping falls back to OTHER.
_CHARGE_CATEGORY_TO_SOURCE = {
    "consultation": RevenueSource.CLINIC_VISIT,
    "procedure": RevenueSource.CLINIC_PROCEDURE,
    "service": RevenueSource.CLINIC_PROCEDURE,
    "medication": RevenueSource.PHARMACY_SALE,
    "lab_test": RevenueSource.LAB_TEST,
    "bed": RevenueSource.OTHER,
    "device": RevenueSource.DEVICE_INCOME,
    "other": RevenueSource.OTHER,
}


def _bridge_doctor(charge_item, clinic):
    """doctor = charge_item.performer, fallback: the clinic owner."""
    return charge_item.performer or clinic.owner


def _upsert_bridge_entry(charge_item, *, clinic, request=None) -> RevenueEntry:
    """
    One bridged RevenueEntry per charge item, keyed on the `charge_item`
    OneToOne. A genuine upsert, not create-if-absent: re-issuing a charge
    item that was previously CANCELLED must refresh the same
    row back to a fresh PENDING state, not leave it stuck CANCELLED
    forever ("re-issuing re-bridges fresh entries").
    """
    doctor = _bridge_doctor(charge_item, clinic)
    share_rule = get_active_share_rule(clinic, doctor)
    source_type = _CHARGE_CATEGORY_TO_SOURCE.get(charge_item.category, RevenueSource.OTHER)

    entry = RevenueEntry.objects.filter(charge_item=charge_item).first()
    is_new = entry is None
    if entry is None:
        entry = RevenueEntry(charge_item=charge_item)

    entry.doctor = doctor
    entry.owner = clinic.owner if share_rule is not None else None
    entry.business_unit = _clinic_business_unit(clinic)
    entry.clinic = clinic
    entry.source_type = source_type
    entry.amount = charge_item.total_amount
    entry.amount_received = Decimal("0")
    entry.currency = charge_item.currency
    entry.status = EntryStatus.PENDING
    entry.occurred_on = charge_item.service_date
    entry.settled_on = None
    entry.split_enabled = share_rule is not None
    entry.doctor_share_percentage = (
        share_rule.doctor_share_percentage if share_rule is not None else None
    )
    entry.patient = charge_item.patient
    entry.recorded_by = doctor
    entry.save()
    log_financial_event(
        request,
        FinancialEvent.ENTRY_CREATED if is_new else FinancialEvent.ENTRY_UPDATED,
        actor=doctor,
        object_type="revenue_entry",
        object_id=str(entry.external_id),
        clinic=clinic,
        amount=entry.amount,
        metadata={"source_type": source_type, "bridged": True},
    )
    return entry


def record_billing_payment(
    invoice, *, event: str, charge_items=None, payment=None, request=None
) -> list[RevenueEntry]:
    """
    The billing -> finance bridge. Called by apps.billing.services after
    every billing mutation that changes what a doctor has actually earned
    or collected — always via a function-local import at the call site
    (apps.billing never imports apps.finance at module level either).
    Idempotent per charge item.

    `event`:
      - "issued": one BILLED charge item -> one upserted PENDING bridge
        entry each. Covers the insurance case too — the
        invoice is issued with no Payment ever posted, so the entry must
        already exist (PENDING) rather than waiting for a payment event
        that may never come.
      - "paid": `payment.amount` is distributed pro-rata across the
        invoice's covered charge items, by each item's share of
        `invoice.total_net`, quantized to 2dp with the last item taking
        the remainder so the allocations always sum exactly to the
        payment. Added to each
        entry's `amount_received`; status re-derives.
      - "refunded": marks the covered entries REFUNDED — a terminal
        status set explicitly, `amount_received` left as the historical
        fact of what was actually collected before the refund (mirrors
        RevenueEntry's own explicit-terminal-status discipline).
      - "cancelled": marks the covered entries CANCELLED. `charge_items`
        must be passed explicitly here — by the time this fires,
        billing.services.cancel_invoice has already cleared the items'
        `invoice` FK, so they can no longer be found via `invoice.charge_items`.

    Entries created by "issued" are looked up (not recreated) by every
    later event, keyed on `charge_item_id` — an event fired before its
    matching "issued" bridge entry exists is a no-op for that item rather
    than an error, since the payment/refund/cancel-side rollups
    (Invoice/PatientAccount) are already the authoritative numbers
    regardless of what the ledger mirror does with them.
    """
    items = charge_items if charge_items is not None else list(invoice.charge_items.all())
    items = sorted(items, key=lambda item: item.pk)
    if not items:
        return []

    clinic = invoice.clinic

    if event == "issued":
        return [_upsert_bridge_entry(item, clinic=clinic, request=request) for item in items]

    entry_by_charge_item = {
        entry.charge_item_id: entry
        for entry in RevenueEntry.objects.filter(
            charge_item_id__in=[item.pk for item in items], deleted=False
        )
    }

    if event == "cancelled":
        entries = []
        for item in items:
            entry = entry_by_charge_item.get(item.pk)
            if entry is None:
                continue
            entry.status = EntryStatus.CANCELLED
            entry.save(update_fields=["status"])
            log_financial_event(
                request,
                FinancialEvent.ENTRY_CANCELLED,
                actor=entry.recorded_by,
                object_type="revenue_entry",
                object_id=str(entry.external_id),
                clinic=clinic,
                metadata={"reason": "invoice_cancelled"},
            )
            entries.append(entry)
        return entries

    if event == "refunded":
        entries = []
        for item in items:
            entry = entry_by_charge_item.get(item.pk)
            if entry is None:
                continue
            entry.status = EntryStatus.REFUNDED
            entry.save(update_fields=["status"])
            log_financial_event(
                request,
                FinancialEvent.ENTRY_UPDATED,
                actor=entry.recorded_by,
                object_type="revenue_entry",
                object_id=str(entry.external_id),
                clinic=clinic,
                metadata={"reason": "payment_refunded"},
            )
            entries.append(entry)
        return entries

    if event == "paid":
        if payment is None or not invoice.total_net:
            return []
        entries = []
        remaining = quantize2(payment.amount)
        last_index = len(items) - 1
        for index, item in enumerate(items):
            entry = entry_by_charge_item.get(item.pk)
            if entry is None:
                continue
            if index == last_index:
                share = remaining
            else:
                share = quantize2(item.total_amount / invoice.total_net * payment.amount)
                remaining -= share
            # Clamped defensively — the CheckConstraint on RevenueEntry
            # (amount_received <= amount) would otherwise be the first to
            # notice a rounding slip across repeated partial payments.
            entry.amount_received = quantize2(min(entry.amount, entry.amount_received + share))
            entry.status = derive_entry_status(entry.amount, entry.amount_received)
            if entry.status == EntryStatus.RECEIVED and entry.settled_on is None:
                entry.settled_on = clinic_localdate(clinic) if clinic else timezone.localdate()
            entry.save(update_fields=["amount_received", "status", "settled_on"])
            log_financial_event(
                request,
                (
                    FinancialEvent.ENTRY_SETTLED
                    if entry.status == EntryStatus.RECEIVED
                    else FinancialEvent.ENTRY_UPDATED
                ),
                actor=entry.recorded_by,
                object_type="revenue_entry",
                object_id=str(entry.external_id),
                clinic=clinic,
                amount=share,
            )
            entries.append(entry)
        return entries

    raise ValueError(f"Unknown bridge event: {event!r}")


def create_manual_entry(*, doctor, validated_data: dict, request=None) -> RevenueEntry:
    """
    The single write path for `POST finance/entries/`. Derives `clinic`,
    stamps `currency`/`status`, and never
    lets a caller set `visit`/`charge_item`/`split_*`/`owner`/`status`
    directly — those are exclusively written by the billing bridge
    (`record_billing_payment`).
    """
    business_unit = validated_data.get("business_unit")
    clinic = resolve_entry_attribution(
        business_unit=business_unit, clinic=validated_data.get("clinic")
    )
    amount = validated_data["amount"]
    amount_received = validated_data.get("amount_received", Decimal("0"))
    if amount_received > amount:
        raise ValidationError({"amount_received": "Cannot exceed amount."})

    # Explicit value wins; otherwise the clinic's own local date when a
    # clinic is attributable, else today's process-wide local date. Always
    # resolved to a concrete date — the field isn't nullable, and this way
    # settled_on (below) never has to guess either.
    occurred_on = validated_data.get("occurred_on")
    if occurred_on is None:
        occurred_on = clinic_localdate(clinic) if clinic is not None else timezone.localdate()

    # Fully received at creation time -> settled on the date it occurred
    # (whatever the caller stated, possibly backdated), not "today" —
    # there's no separate later-collection event distinct from the entry
    # being recorded already-settled. A later pending->received transition
    # (see update_manual_entry) is the case where "today" is correct.
    status = derive_entry_status(amount, amount_received)
    settled_on = occurred_on if status == EntryStatus.RECEIVED else None

    entry = RevenueEntry.objects.create(
        doctor=doctor,
        business_unit=business_unit,
        clinic=clinic,
        source_type=validated_data["source_type"],
        amount=amount,
        amount_received=amount_received,
        currency=DEFAULT_CURRENCY,
        payment_mode=validated_data.get("payment_mode", ""),
        status=status,
        occurred_on=occurred_on,
        settled_on=settled_on,
        patient=validated_data.get("patient"),
        recorded_by=doctor,
        notes=validated_data.get("notes", ""),
        metadata=validated_data.get("metadata", {}),
    )
    log_financial_event(
        request,
        FinancialEvent.ENTRY_CREATED,
        actor=doctor,
        object_type="revenue_entry",
        object_id=str(entry.external_id),
        clinic=clinic,
        amount=amount,
        metadata={"source_type": entry.source_type, "manual": True},
    )
    return entry


def update_manual_entry(
    entry: RevenueEntry, validated_data: dict, *, request=None
) -> RevenueEntry:
    """
    The single write path for `PATCH finance/entries/<id>/`. Visit-linked
    entries are rejected before this is ever called (see the view) —
    this function only ever touches manual entries.
    """
    terminal_status = validated_data.pop("status", None)

    if "business_unit" in validated_data or "clinic" in validated_data:
        business_unit = validated_data.get("business_unit", entry.business_unit)
        clinic = resolve_entry_attribution(
            business_unit=business_unit, clinic=validated_data.get("clinic", entry.clinic)
        )
        entry.business_unit = business_unit
        entry.clinic = clinic

    for field in ("amount", "amount_received", "occurred_on", "payment_mode", "notes", "metadata"):
        if field in validated_data:
            setattr(entry, field, validated_data[field])

    if entry.amount_received > entry.amount:
        raise ValidationError({"amount_received": "Cannot exceed amount."})

    if terminal_status in (EntryStatus.REFUNDED, EntryStatus.CANCELLED):
        entry.status = terminal_status
    else:
        entry.status = derive_entry_status(entry.amount, entry.amount_received)
        if entry.status == EntryStatus.RECEIVED:
            if entry.settled_on is None:
                entry.settled_on = (
                    clinic_localdate(entry.clinic)
                    if entry.clinic is not None
                    else entry.occurred_on
                )
        else:
            # A correction that drops amount_received back below the total
            # means it's no longer true that this was fully received —
            # settled_on should only ever be populated iff status=RECEIVED.
            entry.settled_on = None

    entry.save()
    log_financial_event(
        request,
        (
            FinancialEvent.ENTRY_SETTLED
            if entry.status == EntryStatus.RECEIVED
            else FinancialEvent.ENTRY_UPDATED
        ),
        actor=entry.recorded_by,
        object_type="revenue_entry",
        object_id=str(entry.external_id),
        clinic=entry.clinic,
        amount=entry.amount,
    )
    return entry


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


def record_purchase_expense(order, *, request=None) -> RevenueEntry:
    """
    One EXPENSE-direction entry per received PurchaseOrder — the supply
    cost that funded the clinic's inventory. Called once, from
    apps.clinics.views.PurchaseOrderReceiveView, right after stock is
    incremented; the view's own status guard (only an ORDERED order can
    be received) already makes this a one-shot event, so there's no
    update/idempotency case to handle here the way visit capture needs.

    Expenses are owner-level, never split — a supply cost is the
    clinic's own overhead, not conducting-doctor income, so `doctor` is
    the clinic owner and `owner`/`split_enabled` stay unset (matches
    get_active_share_rule's own short-circuit for the owner-conducting
    case).
    """
    # unit_price is optional on a line item (apps.clinics.serializers) — a
    # line with no known cost contributes nothing rather than crashing the
    # whole expense calculation.
    total = Decimal("0")
    for line in order.items:
        unit_price = line.get("unit_price")
        if unit_price is None:
            continue
        total += Decimal(str(line["quantity"])) * Decimal(str(unit_price))
    amount = quantize2(total)
    occurred_on = clinic_localdate(order.clinic)

    entry = RevenueEntry.objects.create(
        doctor=order.clinic.owner,
        business_unit=_clinic_business_unit(order.clinic),
        clinic=order.clinic,
        source_type=RevenueSource.SUPPLY_PURCHASE,
        direction=EntryDirection.EXPENSE,
        amount=amount,
        amount_received=amount,
        currency=DEFAULT_CURRENCY,
        status=EntryStatus.RECEIVED,
        occurred_on=occurred_on,
        settled_on=occurred_on,
        purchase_order=order,
        recorded_by=order.ordered_by,
    )
    log_financial_event(
        request,
        FinancialEvent.ENTRY_CREATED,
        actor=order.ordered_by,
        object_type="revenue_entry",
        object_id=str(entry.external_id),
        clinic=order.clinic,
        amount=amount,
        metadata={"source_type": RevenueSource.SUPPLY_PURCHASE, "direction": "expense"},
    )
    return entry


# ---------------------------------------------------------------------------
# Clinic-summary aggregation
# ---------------------------------------------------------------------------

_ZERO = Decimal("0.00")


def _clinic_revenue_queryset(clinic_ids):
    return RevenueEntry.objects.filter(
        clinic_id__in=clinic_ids, deleted=False, direction=EntryDirection.INCOME
    ).exclude(status__in=(EntryStatus.CANCELLED, EntryStatus.REFUNDED))


def clinic_total_revenue(clinic_ids) -> Decimal:
    """
    All-time gross revenue across the given clinics, read from the
    ledger — replaces the ad-hoc `Visit.amount_paid` sums that
    apps.clinics.views/apps.patients.views once computed. Every income
    entry a clinic has ever generated is here regardless of which
    capture path wrote it (historical visit-linked, billing-bridged, or
    manual), so this is a strict superset of what the old aggregation
    could see — and, unlike the old one, correctly excludes refunded
    money instead of counting it forever.
    """
    total = _clinic_revenue_queryset(clinic_ids).aggregate(total=Sum("amount"))["total"]
    return quantize2(total) if total is not None else _ZERO


def clinic_monthly_revenue(clinic_ids, *, year: int, month: int) -> Decimal:
    """Same as `clinic_total_revenue`, scoped to one calendar month."""
    total = (
        _clinic_revenue_queryset(clinic_ids)
        .filter(occurred_on__year=year, occurred_on__month=month)
        .aggregate(total=Sum("amount"))["total"]
    )
    return quantize2(total) if total is not None else _ZERO


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _viewer_share_expression(viewer_id: int, field: str):
    """
    A Case/When expression computing `viewer_id`'s share of `field`
    (amount or amount_received) for each row:
      - unsplit entry -> the whole field value
      - viewer is the entry's doctor -> their configured percentage
      - viewer is the entry's owner (the only other party a row can
        belong to) -> the remainder

    Mirrors RevenueEntry.doctor_share_amount/owner_share_amount exactly
    (rounded doctor share; owner share is the remainder, never
    independently rounded) so the dashboard's viewer-relative numbers
    never disagree with what a single entry's own serializer shows.
    """
    rounded_doctor_share = Round(
        F(field) * F("doctor_share_percentage") / Value(Decimal("100")), 2
    )
    return Case(
        When(split_enabled=False, then=F(field)),
        When(doctor_id=viewer_id, then=rounded_doctor_share),
        default=F(field) - rounded_doctor_share,
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _dashboard_base_queryset(
    viewer,
    grant,
    *,
    date_from: date,
    date_to: date,
    clinic=None,
    business_unit=None,
    source_type=None,
):
    qs = RevenueEntry.objects.filter(
        Q(doctor=viewer) | Q(owner=viewer),
        deleted=False,
        direction=EntryDirection.INCOME,
        occurred_on__gte=date_from,
        occurred_on__lte=date_to,
    )
    qs = scope_queryset_to_grant(qs, grant)
    if clinic is not None:
        qs = qs.filter(clinic=clinic)
    if business_unit is not None:
        qs = qs.filter(business_unit=business_unit)
    if source_type:
        qs = qs.filter(source_type=source_type)
    return qs.annotate(
        your_amount=_viewer_share_expression(viewer.id, "amount"),
        your_received=_viewer_share_expression(viewer.id, "amount_received"),
    )


_DASHBOARD_ROW_FIELDS = (
    "external_id",
    "source_type",
    "status",
    "occurred_on",
    "payment_mode",
    "business_unit_id",
    "business_unit__name",
    "business_unit__unit_type",
    "business_unit__ownership",
    "doctor_id",
    "owner_id",
    "split_enabled",
    "metadata",
    "your_amount",
    "your_received",
)


def _bucket_key(occurred_on: date, granularity: str) -> date:
    if granularity == "day":
        return occurred_on
    if granularity == "week":
        return occurred_on - timedelta(days=occurred_on.weekday())  # Monday of that week
    return occurred_on.replace(day=1)  # month


def _bucket_range(date_from: date, date_to: date, granularity: str) -> list[date]:
    buckets: list[date] = []
    if granularity == "day":
        step = timedelta(days=1)
        cursor = date_from
        while cursor <= date_to:
            buckets.append(cursor)
            cursor += step
    elif granularity == "week":
        cursor = date_from - timedelta(days=date_from.weekday())
        end = date_to - timedelta(days=date_to.weekday())
        while cursor <= end:
            buckets.append(cursor)
            cursor += timedelta(days=7)
    else:  # month
        cursor = date_from.replace(day=1)
        while cursor <= date_to:
            buckets.append(cursor)
            year = cursor.year + (cursor.month // 12)
            month = cursor.month % 12 + 1
            cursor = cursor.replace(year=year, month=month, day=1)
    return buckets


def _mr_in_kind_value(metadata: dict) -> Decimal:
    total = _ZERO
    for item in (metadata or {}).get("items", []):
        value = item.get("estimated_value")
        if value is not None:
            total += Decimal(str(value))
    return total


def _period_gross(viewer, grant, *, date_from: date, date_to: date, **filters) -> Decimal:
    qs = _dashboard_base_queryset(viewer, grant, date_from=date_from, date_to=date_to, **filters)
    qs = qs.exclude(status__in=(EntryStatus.CANCELLED, EntryStatus.REFUNDED))
    total = _ZERO
    for row in qs.values_list("your_amount", flat=True):
        total += row
    return quantize2(total)


def _period_expenses(
    viewer, grant, *, date_from: date, date_to: date, clinic=None, business_unit=None
) -> Decimal:
    """
    Expenses are always owner-level and never split (`doctor` is
    always the clinic owner, `owner`/`split_enabled` are never set — see
    record_purchase_expense) — so unlike income there is no
    doctor-vs-owner share to compute, just a straight sum of what this
    viewer themselves incurred as a clinic owner.
    """
    qs = RevenueEntry.objects.filter(
        doctor=viewer,
        deleted=False,
        direction=EntryDirection.EXPENSE,
        occurred_on__gte=date_from,
        occurred_on__lte=date_to,
    ).exclude(status__in=(EntryStatus.CANCELLED, EntryStatus.REFUNDED))
    qs = scope_queryset_to_grant(qs, grant)
    if clinic is not None:
        qs = qs.filter(clinic=clinic)
    if business_unit is not None:
        qs = qs.filter(business_unit=business_unit)
    total = qs.aggregate(total=Sum("amount"))["total"] or _ZERO
    return quantize2(total)


def compute_dashboard(
    viewer,
    grant,
    *,
    date_from: date,
    date_to: date,
    granularity: str,
    clinic=None,
    business_unit=None,
    source_type=None,
) -> dict:
    """
    The single source of the finance dashboard response. One query fetches every entry in
    range with its per-row viewer share already computed by the DB
    (Case/When); every breakdown below is a single Python pass over those
    rows rather than N separate GROUP BY queries — cheaper and simpler
    than round-tripping once per block, at the entry volumes this system
    expects (thousands/year per doctor).
    """
    qs = _dashboard_base_queryset(
        viewer,
        grant,
        date_from=date_from,
        date_to=date_to,
        clinic=clinic,
        business_unit=business_unit,
        source_type=source_type,
    )
    rows = list(qs.exclude(status=EntryStatus.CANCELLED).values(*_DASHBOARD_ROW_FIELDS))
    expenses = _period_expenses(
        viewer,
        grant,
        date_from=date_from,
        date_to=date_to,
        clinic=clinic,
        business_unit=business_unit,
    )

    money_rows = [r for r in rows if r["status"] != EntryStatus.REFUNDED]
    refunded_rows = [r for r in rows if r["status"] == EntryStatus.REFUNDED]

    gross = quantize2(sum((r["your_amount"] for r in money_rows), _ZERO))
    received = quantize2(sum((r["your_received"] for r in money_rows), _ZERO))
    refunded = quantize2(sum((r["your_amount"] for r in refunded_rows), _ZERO))

    # --- by_status --------------------------------------------------------
    by_status_amount: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    by_status_received: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    by_status_count: dict[str, int] = defaultdict(int)
    for r in rows:
        by_status_amount[r["status"]] += r["your_amount"]
        by_status_received[r["status"]] += r["your_received"]
        by_status_count[r["status"]] += 1

    by_status = []
    for entry_status in (
        EntryStatus.RECEIVED,
        EntryStatus.PARTIAL,
        EntryStatus.PENDING,
        EntryStatus.REFUNDED,
    ):
        if by_status_count[entry_status] == 0:
            continue
        block = {
            "status": entry_status,
            "amount": quantize2(by_status_amount[entry_status]),
            "count": by_status_count[entry_status],
        }
        if entry_status == EntryStatus.PARTIAL:
            block["received"] = quantize2(by_status_received[entry_status])
        by_status.append(block)

    # --- by_source ----------------------------------------------------------
    by_source_amount: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    by_source_count: dict[str, int] = defaultdict(int)
    by_source_in_kind: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for r in money_rows:
        by_source_amount[r["source_type"]] += r["your_amount"]
        by_source_count[r["source_type"]] += 1
        if r["source_type"] == RevenueSource.MR_ENGAGEMENT:
            by_source_in_kind[r["source_type"]] += _mr_in_kind_value(r["metadata"])

    # "Zero-activity sources omitted" needs no explicit filter here —
    # by_source_amount is a defaultdict populated only inside the loop
    # over money_rows above, so every key present already has count >= 1;
    # a RevenueSource with no entries in range is simply never inserted.
    by_source = []
    for source_type_key, amount in sorted(
        by_source_amount.items(), key=lambda item: item[1], reverse=True
    ):
        block = {
            "source_type": source_type_key,
            "amount": quantize2(amount),
            "count": by_source_count[source_type_key],
            "pct": float(round(amount / gross * 100, 1)) if gross else 0.0,
        }
        if by_source_in_kind[source_type_key]:
            block["in_kind_estimated_value"] = quantize2(by_source_in_kind[source_type_key])
        by_source.append(block)

    # --- by_business_unit -----------------------------------------------
    unit_amount: dict[int | None, Decimal] = defaultdict(lambda: _ZERO)
    unit_count: dict[int | None, int] = defaultdict(int)
    unit_detail: dict[int | None, dict] = {}
    for r in money_rows:
        key = r["business_unit_id"]
        unit_amount[key] += r["your_amount"]
        unit_count[key] += 1
        unit_detail.setdefault(
            key,
            {
                "name": r["business_unit__name"],
                "unit_type": r["business_unit__unit_type"],
                "ownership": r["business_unit__ownership"],
            },
        )
    # Map internal business_unit_id -> external_id for the response, one
    # query for whichever units actually appeared.
    unit_ids = [key for key in unit_amount if key is not None]
    external_id_by_pk = dict(
        BusinessUnit.objects.filter(pk__in=unit_ids).values_list("pk", "external_id")
    )
    by_business_unit = []
    for key, amount in unit_amount.items():
        detail = unit_detail[key]
        by_business_unit.append(
            {
                "external_id": str(external_id_by_pk[key]) if key is not None else None,
                "name": detail["name"] if key is not None else None,
                "unit_type": detail["unit_type"] if key is not None else None,
                "ownership": detail["ownership"] if key is not None else None,
                "amount": quantize2(amount),
                "count": unit_count[key],
            }
        )
    by_business_unit.sort(key=lambda b: b["amount"], reverse=True)

    # --- shared -----------------------------------------------------------
    earned_as_doctor_amount = _ZERO
    earned_as_doctor_count = 0
    earned_as_owner_amount = _ZERO
    earned_as_owner_count = 0
    for r in money_rows:
        if not r["split_enabled"]:
            continue
        if r["doctor_id"] == viewer.id:
            earned_as_doctor_amount += r["your_amount"]
            earned_as_doctor_count += 1
        if r["owner_id"] == viewer.id:
            earned_as_owner_amount += r["your_amount"]
            earned_as_owner_count += 1

    # --- by_payment_mode --------------------------------------------------
    mode_amount: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    mode_count: dict[str, int] = defaultdict(int)
    for r in money_rows:
        if not r["payment_mode"]:
            continue
        mode_amount[r["payment_mode"]] += r["your_amount"]
        mode_count[r["payment_mode"]] += 1
    by_payment_mode = [
        {"payment_mode": mode, "amount": quantize2(amount), "count": mode_count[mode]}
        for mode, amount in sorted(mode_amount.items(), key=lambda item: item[1], reverse=True)
    ]

    # --- timeseries (zero-filled) -------------------------------------------
    bucket_amount: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    bucket_received: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    bucket_count: dict[date, int] = defaultdict(int)
    for r in money_rows:
        key = _bucket_key(r["occurred_on"], granularity)
        bucket_amount[key] += r["your_amount"]
        bucket_received[key] += r["your_received"]
        bucket_count[key] += 1
    timeseries = [
        {
            "bucket": bucket.isoformat(),
            "amount": quantize2(bucket_amount[bucket]),
            "received": quantize2(bucket_received[bucket]),
            "count": bucket_count[bucket],
        }
        for bucket in _bucket_range(date_from, date_to, granularity)
    ]

    # --- outstanding_items --------------------------------------------------
    outstanding_rows = [
        r for r in money_rows if r["status"] in (EntryStatus.PENDING, EntryStatus.PARTIAL)
    ]
    outstanding_rows.sort(key=lambda r: r["occurred_on"])
    today = timezone.localdate()
    outstanding_items = [
        {
            "external_id": str(r["external_id"]),
            "source_type": r["source_type"],
            "amount": quantize2(r["your_amount"]),
            "amount_received": quantize2(r["your_received"]),
            "outstanding": quantize2(r["your_amount"] - r["your_received"]),
            "occurred_on": r["occurred_on"].isoformat(),
            "days_outstanding": (today - r["occurred_on"]).days,
        }
        for r in outstanding_rows[:10]
    ]

    # --- previous_period ----------------------------------------------------
    span = (date_to - date_from) + timedelta(days=1)
    previous_to = date_from - timedelta(days=1)
    previous_from = previous_to - span + timedelta(days=1)
    previous_gross = _period_gross(
        viewer,
        grant,
        date_from=previous_from,
        date_to=previous_to,
        clinic=clinic,
        business_unit=business_unit,
        source_type=source_type,
    )
    delta_pct = (
        float(round((gross - previous_gross) / previous_gross * 100, 1))
        if previous_gross
        else None
    )

    return {
        "period": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "granularity": granularity,
        },
        "viewer": {
            "doctor": str(viewer.external_id),
            "via_grant": grant is not None,
        },
        "totals": {
            "gross": gross,
            "received": received,
            "outstanding": quantize2(gross - received),
            "refunded": refunded,
            "entry_count": len(money_rows),
            "expenses": expenses,
            "net": quantize2(received - expenses),
        },
        "by_status": by_status,
        "by_source": by_source,
        "by_business_unit": by_business_unit,
        "shared": {
            "earned_as_doctor": {
                "gross_share": quantize2(earned_as_doctor_amount),
                "entries": earned_as_doctor_count,
            },
            "earned_as_owner": {
                "gross_share": quantize2(earned_as_owner_amount),
                "entries": earned_as_owner_count,
            },
        },
        "by_payment_mode": by_payment_mode,
        "timeseries": timeseries,
        "outstanding_items": outstanding_items,
        "previous_period": {
            "from": previous_from.isoformat(),
            "to": previous_to.isoformat(),
            "gross": previous_gross,
            "delta_pct": delta_pct,
        },
    }
