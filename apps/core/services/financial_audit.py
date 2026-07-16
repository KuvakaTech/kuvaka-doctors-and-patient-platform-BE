"""
Financial audit logging service — the money counterpart of
apps.core.services.audit (auth events).

apps.finance and apps.billing call `log_financial_event` from every
service function that mutates money, so call sites stay clean and
persistence detail is centralised here.
"""

import logging

# Re-exported for callers — apps.billing/apps.finance import FinancialEvent
# from this module rather than reaching into apps.core.models directly.
from apps.core.models import FinancialAuditLog, FinancialEvent  # noqa: F401

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str | None:
    """Mirrors apps.core.services.audit._get_client_ip — honours X-Forwarded-For."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_financial_event(
    request,
    event: str,
    *,
    actor,
    object_type: str,
    object_id: str,
    clinic=None,
    amount=None,
    metadata: dict | None = None,
) -> None:
    """
    Write one financial audit record. Never raises — a logging failure must
    not break the money mutation it's recording, but it is logged as an
    error so ops can alert on it (same discipline as log_auth_event).

    Args:
        request:     The DRF/Django request (for IP extraction). Pass None
                     for system-initiated writes (backfills, scheduled jobs).
        event:       One of the FinancialEvent choices.
        actor:       The User performing the action (None only for system writes).
        object_type: A short label for the mutated model, e.g. "revenue_entry".
        object_id:   The object's external_id (str) — never the internal PK.
        clinic:      The Clinic this event is scoped to, if any.
        amount:      The money amount involved, if applicable.
        metadata:    Extra context — before/after values, reasons. Never
                     credentials; never patient names or clinical content,
                     identifiers only.
    """
    try:
        FinancialAuditLog.objects.create(
            actor=actor,
            event=event,
            clinic=clinic,
            object_type=object_type,
            object_id=str(object_id),
            amount=amount,
            metadata=metadata or {},
            ip_address=_get_client_ip(request) if request is not None else None,
        )
    except Exception:
        logger.exception(
            "Failed to write financial audit log for event=%s object=%s:%s",
            event,
            object_type,
            object_id,
        )
