from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.clinics.models import Clinic
from apps.finance.models import BusinessUnit, FinanceAccessGrant, RevenueEntry
from apps.finance.services import compute_dashboard
from apps.users.models import User

# `days_outstanding` is computed against the real wall-clock "today"
# (apps.finance.services.compute_dashboard), so TODAY must track it too —
# a hardcoded past/future date drifts by exactly one day every day this
# suite isn't run, silently breaking test_outstanding_items_oldest_first_capped_at_ten.
TODAY = timezone.localdate()


@pytest.fixture
def doctor(db):
    return User.objects.create_user(email="doctor@example.com", password="pw", user_type="doctor")


@pytest.fixture
def other_doctor(db):
    return User.objects.create_user(email="other@example.com", password="pw", user_type="doctor")


@pytest.fixture
def clinic(doctor):
    return Clinic.objects.create(name="Sharma Clinic", owner=doctor)


def _entry(**kwargs):
    defaults = {
        "amount_received": Decimal("0"),
        "status": "pending",
        "occurred_on": TODAY,
    }
    defaults.update(kwargs)
    return RevenueEntry.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Basic totals
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_gross_and_received_totals_unsplit(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("500.00"),
        amount_received=Decimal("500.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("300.00"),
        amount_received=Decimal("100.00"),
        status="partial",
    )

    result = compute_dashboard(
        doctor, None, date_from=TODAY - timedelta(days=1), date_to=TODAY, granularity="day"
    )
    assert result["totals"]["gross"] == Decimal("800.00")
    assert result["totals"]["received"] == Decimal("600.00")
    assert result["totals"]["outstanding"] == Decimal("200.00")
    assert result["totals"]["entry_count"] == 2


@pytest.mark.django_db
def test_cancelled_entries_excluded_entirely(doctor):
    _entry(doctor=doctor, source_type="home_visit", amount=Decimal("500.00"), status="cancelled")
    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert result["totals"]["gross"] == Decimal("0.00")
    assert result["totals"]["entry_count"] == 0
    assert result["by_status"] == []


@pytest.mark.django_db
def test_refunded_shown_separately_never_netted(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("500.00"),
        amount_received=Decimal("500.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("200.00"),
        amount_received=Decimal("200.00"),
        status="refunded",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    # The refunded entry does not subtract from gross — gross only ever
    # covers pending/partial/received rows.
    assert result["totals"]["gross"] == Decimal("500.00")
    assert result["totals"]["refunded"] == Decimal("200.00")


# ---------------------------------------------------------------------------
# Split entries — viewer-relative
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_split_entry_worked_example(doctor, other_doctor):
    # The canonical revenue-share worked example:
    # Rs 1000 consult, 60% to the conducting doctor, 40% to the clinic owner.
    entry = RevenueEntry.objects.create(
        doctor=other_doctor,
        owner=doctor,
        source_type="clinic_visit",
        amount=Decimal("1000.00"),
        amount_received=Decimal("1000.00"),
        status="received",
        occurred_on=TODAY,
        split_enabled=True,
        doctor_share_percentage=Decimal("60.00"),
    )

    as_conductor = compute_dashboard(
        other_doctor, None, date_from=TODAY, date_to=TODAY, granularity="day"
    )
    assert as_conductor["totals"]["gross"] == Decimal("600.00")

    as_owner = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert as_owner["totals"]["gross"] == Decimal("400.00")

    # Both sides must sum back to the entry's real total, always.
    assert as_conductor["totals"]["gross"] + as_owner["totals"]["gross"] == entry.amount


@pytest.mark.django_db
def test_shared_block_earned_as_doctor_and_owner(doctor, other_doctor, clinic):
    # doctor is the owner earning a remainder at their own clinic (via a
    # split with other_doctor); doctor is also, separately, the visiting
    # conductor at some other clinic owned by other_doctor.
    other_clinic = Clinic.objects.create(name="Other Clinic", owner=other_doctor)

    RevenueEntry.objects.create(
        doctor=other_doctor,
        owner=doctor,
        clinic=clinic,
        source_type="clinic_visit",
        amount=Decimal("1000.00"),
        amount_received=Decimal("1000.00"),
        status="received",
        occurred_on=TODAY,
        split_enabled=True,
        doctor_share_percentage=Decimal("60.00"),
    )
    RevenueEntry.objects.create(
        doctor=doctor,
        owner=other_doctor,
        clinic=other_clinic,
        source_type="clinic_visit",
        amount=Decimal("500.00"),
        amount_received=Decimal("500.00"),
        status="received",
        occurred_on=TODAY,
        split_enabled=True,
        doctor_share_percentage=Decimal("70.00"),
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert result["shared"]["earned_as_owner"]["gross_share"] == Decimal("400.00")
    assert result["shared"]["earned_as_owner"]["entries"] == 1
    assert result["shared"]["earned_as_doctor"]["gross_share"] == Decimal("350.00")
    assert result["shared"]["earned_as_doctor"]["entries"] == 1


# ---------------------------------------------------------------------------
# by_status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_by_status_breakdown_includes_partial_received_subfield(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("1000.00"),
        amount_received=Decimal("400.00"),
        status="partial",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    partial_block = next(b for b in result["by_status"] if b["status"] == "partial")
    assert partial_block["amount"] == Decimal("1000.00")
    assert partial_block["received"] == Decimal("400.00")
    assert partial_block["count"] == 1


# ---------------------------------------------------------------------------
# by_source + MR in-kind
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_by_source_percentages(doctor):
    _entry(
        doctor=doctor,
        source_type="clinic_visit",
        amount=Decimal("750.00"),
        amount_received=Decimal("750.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("250.00"),
        amount_received=Decimal("250.00"),
        status="received",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    by_source = {b["source_type"]: b for b in result["by_source"]}
    assert by_source["clinic_visit"]["pct"] == 75.0
    assert by_source["home_visit"]["pct"] == 25.0


@pytest.mark.django_db
def test_mr_in_kind_value_excluded_from_money_totals(doctor):
    # Zero-amount entries are RECEIVED, not PENDING — see derive_entry_status.
    _entry(
        doctor=doctor,
        source_type="mr_engagement",
        amount=Decimal("0"),
        amount_received=Decimal("0"),
        status="received",
        metadata={
            "mr_company": "Sun Pharma",
            "items": [
                {"kind": "samples", "description": "Azithral", "estimated_value": "1500.00"},
                {"kind": "gift", "description": "Bag", "estimated_value": "800.00"},
            ],
        },
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert result["totals"]["gross"] == Decimal("0.00")
    mr_block = next(b for b in result["by_source"] if b["source_type"] == "mr_engagement")
    assert mr_block["amount"] == Decimal("0.00")
    assert mr_block["in_kind_estimated_value"] == Decimal("2300.00")


@pytest.mark.django_db
def test_by_source_omits_in_kind_key_when_no_items(doctor):
    _entry(
        doctor=doctor,
        source_type="mr_engagement",
        amount=Decimal("2000.00"),
        amount_received=Decimal("2000.00"),
        status="received",
    )
    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    mr_block = next(b for b in result["by_source"] if b["source_type"] == "mr_engagement")
    assert "in_kind_estimated_value" not in mr_block


# ---------------------------------------------------------------------------
# by_business_unit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_by_business_unit_includes_unattributed_bucket(doctor, clinic):
    unit = BusinessUnit.objects.create(
        owner=doctor, clinic=clinic, name=clinic.name, unit_type="clinic"
    )
    _entry(
        doctor=doctor,
        business_unit=unit,
        clinic=clinic,
        source_type="clinic_visit",
        amount=Decimal("500.00"),
        amount_received=Decimal("500.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    by_unit = {row["external_id"]: row for row in result["by_business_unit"]}
    assert by_unit[str(unit.external_id)]["amount"] == Decimal("500.00")
    assert by_unit[str(unit.external_id)]["name"] == clinic.name
    assert by_unit[None]["amount"] == Decimal("100.00")
    assert by_unit[None]["name"] is None


# ---------------------------------------------------------------------------
# by_payment_mode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_by_payment_mode_omits_blank_mode(doctor):
    _entry(
        doctor=doctor,
        source_type="clinic_visit",
        amount=Decimal("500.00"),
        amount_received=Decimal("500.00"),
        status="received",
        payment_mode="cash",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
        payment_mode="",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    modes = {b["payment_mode"]: b["amount"] for b in result["by_payment_mode"]}
    assert modes == {"cash": Decimal("500.00")}


# ---------------------------------------------------------------------------
# timeseries (zero-filled)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_timeseries_zero_fills_days_with_no_activity(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
        occurred_on=TODAY,
    )

    result = compute_dashboard(
        doctor, None, date_from=TODAY - timedelta(days=2), date_to=TODAY, granularity="day"
    )
    assert len(result["timeseries"]) == 3
    buckets = {b["bucket"]: b for b in result["timeseries"]}
    assert buckets[(TODAY - timedelta(days=2)).isoformat()]["amount"] == Decimal("0.00")
    assert buckets[(TODAY - timedelta(days=2)).isoformat()]["count"] == 0
    assert buckets[TODAY.isoformat()]["amount"] == Decimal("100.00")
    assert buckets[TODAY.isoformat()]["count"] == 1


@pytest.mark.django_db
def test_timeseries_month_granularity_buckets_by_month_start(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
        occurred_on=date(2026, 6, 15),
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("200.00"),
        amount_received=Decimal("200.00"),
        status="received",
        occurred_on=date(2026, 7, 1),
    )

    result = compute_dashboard(
        doctor, None, date_from=date(2026, 6, 1), date_to=date(2026, 7, 15), granularity="month"
    )
    buckets = {b["bucket"]: b["amount"] for b in result["timeseries"]}
    assert buckets["2026-06-01"] == Decimal("100.00")
    assert buckets["2026-07-01"] == Decimal("200.00")


# ---------------------------------------------------------------------------
# outstanding_items
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_outstanding_items_oldest_first_capped_at_ten(doctor):
    for days_ago in range(15):
        _entry(
            doctor=doctor,
            source_type="insurance_claim",
            amount=Decimal("100.00"),
            amount_received=Decimal("0"),
            status="pending",
            occurred_on=TODAY - timedelta(days=days_ago),
        )

    result = compute_dashboard(
        doctor, None, date_from=TODAY - timedelta(days=20), date_to=TODAY, granularity="day"
    )
    items = result["outstanding_items"]
    assert len(items) == 10
    # Oldest first — "chase these".
    assert items[0]["occurred_on"] == (TODAY - timedelta(days=14)).isoformat()
    assert items[0]["days_outstanding"] == 14
    assert items[-1]["days_outstanding"] == 5


@pytest.mark.django_db
def test_outstanding_items_excludes_received_and_refunded(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("50.00"),
        amount_received=Decimal("50.00"),
        status="refunded",
    )

    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert result["outstanding_items"] == []


# ---------------------------------------------------------------------------
# previous_period
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_previous_period_window_and_delta(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("200.00"),
        amount_received=Decimal("200.00"),
        status="received",
        occurred_on=date(2026, 7, 10),
    )
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
        occurred_on=date(2026, 6, 20),
    )

    result = compute_dashboard(
        doctor, None, date_from=date(2026, 7, 1), date_to=date(2026, 7, 15), granularity="day"
    )
    assert result["previous_period"]["from"] == "2026-06-16"
    assert result["previous_period"]["to"] == "2026-06-30"
    assert result["previous_period"]["gross"] == Decimal("100.00")
    assert result["totals"]["gross"] == Decimal("200.00")
    assert result["previous_period"]["delta_pct"] == 100.0


@pytest.mark.django_db
def test_previous_period_delta_none_when_no_baseline(doctor):
    _entry(
        doctor=doctor,
        source_type="home_visit",
        amount=Decimal("200.00"),
        amount_received=Decimal("200.00"),
        status="received",
        occurred_on=TODAY,
    )
    result = compute_dashboard(doctor, None, date_from=TODAY, date_to=TODAY, granularity="day")
    assert result["previous_period"]["gross"] == Decimal("0.00")
    assert result["previous_period"]["delta_pct"] is None


# ---------------------------------------------------------------------------
# Grantee mode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_via_grant_scopes_to_clinic(doctor, other_doctor, clinic):
    other_clinic = Clinic.objects.create(name="Other", owner=doctor)
    _entry(
        doctor=doctor,
        clinic=clinic,
        source_type="clinic_visit",
        amount=Decimal("100.00"),
        amount_received=Decimal("100.00"),
        status="received",
    )
    _entry(
        doctor=doctor,
        clinic=other_clinic,
        source_type="clinic_visit",
        amount=Decimal("900.00"),
        amount_received=Decimal("900.00"),
        status="received",
    )

    FinanceAccessGrant.objects.create(doctor=doctor, grantee=other_doctor, clinic=clinic)

    from apps.finance.permissions import resolve_finance_viewer

    resolved_doctor, resolved_grant = resolve_finance_viewer(other_doctor, str(doctor.external_id))
    result = compute_dashboard(
        resolved_doctor, resolved_grant, date_from=TODAY, date_to=TODAY, granularity="day"
    )
    assert result["totals"]["gross"] == Decimal("100.00")
    assert result["viewer"]["via_grant"] is True
