from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.clinics.models import Clinic
from apps.core.money import DEFAULT_CURRENCY, PaymentMode, clinic_localdate, quantize2
from apps.users.models import User


def test_payment_mode_re_exported_from_clinical_matches_core():
    # apps.clinical.models.PaymentMode is a re-export (see that module's
    # import comment) — moving the enum to apps.core must not change its
    # values or break either import path.
    from apps.clinical.models import PaymentMode as ClinicalPaymentMode

    assert ClinicalPaymentMode is PaymentMode
    assert set(PaymentMode.values) == {"cash", "card", "upi", "insurance"}


def test_quantize2_rounds_half_up_not_banker_s_rounding():
    # Decimal's own default quantize() uses ROUND_HALF_EVEN (banker's
    # rounding), which would round 2.345 down to 2.34. Currency rounding
    # expects half-up: 2.345 -> 2.35. This is the whole reason quantize2
    # exists instead of calling .quantize() directly at every call site.
    assert quantize2(Decimal("2.345")) == Decimal("2.35")
    assert quantize2(Decimal("2.344")) == Decimal("2.34")
    assert quantize2(Decimal("10")) == Decimal("10.00")
    assert quantize2(Decimal("0")) == Decimal("0.00")


def test_quantize2_returns_decimal_with_two_places():
    result = quantize2(Decimal("100.5"))
    assert result.as_tuple().exponent == -2


def test_default_currency_is_inr():
    # Single-currency assumption today — see the constant's docstring for
    # why this is a named constant rather than a scattered "INR" literal.
    assert DEFAULT_CURRENCY == "INR"


@pytest.mark.django_db
def test_clinic_localdate_uses_clinic_timezone_not_default(monkeypatch):
    owner = User.objects.create_user(email="owner@example.com", password="pw", user_type="doctor")
    ist_clinic = Clinic.objects.create(name="Mumbai Clinic", owner=owner)  # default: Asia/Kolkata
    utc_clinic = Clinic.objects.create(name="UTC Clinic", owner=owner, timezone="UTC")

    # 23:45 UTC on 2026-07-15 is 05:15 IST on 2026-07-16 — different
    # calendar dates depending on which clinic's timezone is used. This is
    # exactly the day-book/fiscal-year bug the global-readiness seam
    # exists to prevent.
    frozen_now = datetime(2026, 7, 15, 23, 45, tzinfo=UTC)
    monkeypatch.setattr("apps.core.money.django_timezone.now", lambda: frozen_now)

    assert clinic_localdate(ist_clinic) == date(2026, 7, 16)
    assert clinic_localdate(utc_clinic) == date(2026, 7, 15)


@pytest.mark.django_db
def test_clinic_timezone_and_fiscal_year_start_month_defaults():
    owner = User.objects.create_user(email="owner2@example.com", password="pw", user_type="doctor")
    clinic = Clinic.objects.create(name="Default Clinic", owner=owner)
    assert clinic.timezone == "Asia/Kolkata"
    assert clinic.fiscal_year_start_month == 4
