"""
Auth audit logging service — HIPAA § 164.312(b).

All security-relevant auth events flow through `log_auth_event` so call sites
stay clean and the persistence detail is centralised here.
"""

import logging

from apps.core.models import AuditLog, AuthEvent  # noqa: F401 — re-export AuthEvent for callers

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str | None:
    """
    Extract the real client IP, honouring X-Forwarded-For when set by a
    trusted reverse proxy. Returns None if the IP can't be determined.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_auth_event(
    request,
    event: str,
    *,
    user=None,
    email: str = "",
    metadata: dict | None = None,
) -> None:
    """
    Write one audit record. Never raises — a logging failure must not break
    the auth flow itself, but it is logged as an error so ops can alert on it.

    Args:
        request:  The DRF/Django request (for IP and user-agent extraction).
        event:    One of the AuthEvent choices.
        user:     The resolved User instance if available (None for failed lookups).
        email:    Raw email from the request — stored even when no user row exists.
        metadata: Optional dict of extra context. Never include credentials here.
    """
    try:
        AuditLog.objects.create(
            user=user,
            event=event,
            email=(email or (user.email if user else "")).lower(),
            ip_address=_get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("Failed to write audit log for event=%s email=%s", event, email)
