"""
Break-glass (emergency access) service — HIPAA § 164.312(a)(2)(ii).

`invoke_break_glass` is the single entry point. It:
  1. Creates an EmergencyAccess record (the permanent trail)
  2. Writes a BREAK_GLASS event to AuditLog
  3. Returns the patient User object so the caller can proceed

`review_break_glass` lets a second admin mark the event as reviewed,
satisfying the post-hoc oversight requirement.

Neither function ever suppresses its own exceptions — a failure to write
the audit trail must surface loudly so it can be investigated.
"""

import logging

from django.utils import timezone

from apps.core.models import AuditLog, AuthEvent, EmergencyAccess

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str | None:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def invoke_break_glass(
    request, *, admin_user, patient_user, justification: str
) -> EmergencyAccess:
    """
    Record a break-glass access and return the EmergencyAccess instance.

    Args:
        request:       Django/DRF request (for IP + user-agent)
        admin_user:    The staff account invoking break-glass (must be is_staff=True)
        patient_user:  The patient whose record is being accessed
        justification: Free-text reason — stored permanently, shown in audit reviews

    Raises:
        PermissionError: if admin_user is not is_staff
        ValueError:      if patient_user is not a patient

    Both exceptions are intentional — callers must not bypass these checks.
    """
    from apps.users.models import UserType

    if not admin_user.is_staff:
        raise PermissionError("Break-glass access requires is_staff=True.")

    if patient_user.user_type != UserType.PATIENT:
        raise ValueError(
            f"Break-glass target must be a patient. Got user_type={patient_user.user_type!r}."
        )

    ip = _get_client_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "")[:512]

    # Permanent EmergencyAccess record
    event = EmergencyAccess.objects.create(
        accessed_by=admin_user,
        patient=patient_user,
        justification=justification.strip(),
        ip_address=ip,
        user_agent=ua,
    )

    # AuditLog cross-reference
    AuditLog.objects.create(
        user=admin_user,
        event=AuthEvent.BREAK_GLASS,
        email=admin_user.email or "",
        ip_address=ip,
        user_agent=ua,
        metadata={
            "patient_id": patient_user.pk,
            "patient_email": patient_user.email or "",
            "emergency_access_id": event.pk,
            "justification": justification.strip(),
        },
    )

    logger.warning(
        "BREAK-GLASS invoked: admin=%s patient=%s ip=%s emergency_access_id=%s",
        admin_user.pk,
        patient_user.pk,
        ip,
        event.pk,
    )

    return event


def review_break_glass(
    *,
    event: EmergencyAccess,
    reviewer: object,
    review_notes: str = "",
) -> EmergencyAccess:
    """
    Mark a break-glass event as reviewed by a second admin.

    Args:
        event:        The EmergencyAccess instance to review
        reviewer:     The staff user performing the review (must differ from accessed_by)
        review_notes: Optional notes from the reviewer

    Raises:
        PermissionError: if reviewer is not is_staff or is the same person as accessed_by
        ValueError:      if the event is already reviewed
    """
    if not reviewer.is_staff:
        raise PermissionError("Reviewing break-glass events requires is_staff=True.")

    if reviewer.pk == event.accessed_by_id:
        raise PermissionError(
            "The reviewer must be a different admin than the one who invoked break-glass."
        )

    if event.is_reviewed:
        raise ValueError(f"EmergencyAccess #{event.pk} has already been reviewed.")

    event.reviewed_by = reviewer
    event.reviewed_at = timezone.now()
    event.review_notes = review_notes.strip()
    event.save(update_fields=["reviewed_by", "reviewed_at", "review_notes"])

    logger.info(
        "BREAK-GLASS reviewed: emergency_access_id=%s reviewer=%s",
        event.pk,
        reviewer.pk,
    )

    return event
