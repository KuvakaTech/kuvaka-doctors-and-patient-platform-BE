from decimal import Decimal
from unittest.mock import Mock

import pytest

from apps.core.models import FinancialAuditLog, FinancialEvent
from apps.core.services.financial_audit import log_financial_event
from apps.users.models import User


def _request(ip="203.0.113.5"):
    request = Mock()
    request.META = {"REMOTE_ADDR": ip, "HTTP_USER_AGENT": "pytest"}
    return request


@pytest.mark.django_db
def test_log_financial_event_persists_all_fields():
    actor = User.objects.create_user(email="doc@example.com", password="pw", user_type="doctor")

    log_financial_event(
        _request(),
        FinancialEvent.ENTRY_CREATED,
        actor=actor,
        object_type="revenue_entry",
        object_id="11111111-1111-1111-1111-111111111111",
        amount=Decimal("1500.00"),
        metadata={"source_type": "clinic_visit"},
    )

    entry = FinancialAuditLog.objects.get()
    assert entry.event == FinancialEvent.ENTRY_CREATED
    assert entry.actor_id == actor.id
    assert entry.object_type == "revenue_entry"
    assert entry.object_id == "11111111-1111-1111-1111-111111111111"
    assert entry.amount == Decimal("1500.00")
    assert entry.metadata == {"source_type": "clinic_visit"}
    assert entry.ip_address == "203.0.113.5"


@pytest.mark.django_db
def test_log_financial_event_allows_null_actor_for_system_writes():
    log_financial_event(
        None,
        FinancialEvent.ENTRY_CREATED,
        actor=None,
        object_type="revenue_entry",
        object_id="22222222-2222-2222-2222-222222222222",
    )

    entry = FinancialAuditLog.objects.get()
    assert entry.actor is None
    assert entry.ip_address is None


@pytest.mark.django_db
def test_log_financial_event_never_raises_on_persistence_failure(monkeypatch):
    # A logging failure must never break the money mutation it's recording
    # (same discipline as apps.core.services.audit.log_auth_event).
    def _boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(FinancialAuditLog.objects, "create", _boom)

    log_financial_event(
        _request(),
        FinancialEvent.PAYMENT_POSTED,
        actor=None,
        object_type="payment",
        object_id="not-even-a-real-id",
    )  # must not raise

    assert not FinancialAuditLog.objects.exists()


@pytest.mark.django_db
def test_financial_audit_log_is_append_only_no_soft_delete_field():
    # FinancialAuditLog deliberately does not inherit BaseModel — it has no
    # `deleted`/`modified_date` fields, matching AuditLog's convention.
    field_names = {f.name for f in FinancialAuditLog._meta.get_fields()}
    assert "deleted" not in field_names
    assert "modified_date" not in field_names
