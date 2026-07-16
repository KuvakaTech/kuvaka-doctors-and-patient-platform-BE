"""
Billing-scoped authorization helpers. Thin wrappers over
apps.clinics.permissions — billing permissions are owner-decided per
clinic exactly like every other PermissionFlag: MANAGE_BILLING and
MANAGE_REFUNDS are eligible for every staff role, and it's the
owner/doctor who decides who actually holds them via the existing
staff-permissions and task-grant APIs. No new enforcement machinery is
needed here — just naming the two checks billing views use.
"""

from rest_framework.exceptions import PermissionDenied

from apps.clinics.models import PermissionFlag
from apps.clinics.permissions import ClinicStaffMembership, has_permission, require_permission
from apps.users.models import UserType


def require_billing_access(user, clinic, *, patient=None) -> ClinicStaffMembership:
    """Capture charges, build/issue invoices, post payments/advances."""
    return require_permission(user, clinic, PermissionFlag.MANAGE_BILLING, patient=patient)


def require_refund_access(user, clinic, *, patient=None) -> ClinicStaffMembership:
    """Cancel issued invoices, post refunds — split from MANAGE_BILLING as higher-trust."""
    return require_permission(user, clinic, PermissionFlag.MANAGE_REFUNDS, patient=patient)


def can_view_billing(user, clinic) -> bool:
    """Read access to accounts/invoices/day-book: MANAGE_BILLING, MANAGE_REFUNDS,
    or VIEW_REVENUE."""
    return (
        has_permission(user, clinic, PermissionFlag.MANAGE_BILLING)
        or has_permission(user, clinic, PermissionFlag.MANAGE_REFUNDS)
        or has_permission(user, clinic, PermissionFlag.VIEW_REVENUE)
    )


def require_view_billing(user, clinic) -> None:
    if not can_view_billing(user, clinic):
        raise PermissionDenied("You do not have permission to view billing data at this clinic.")


def require_patient_owner(user, account) -> None:
    """The 'my accounts/invoices/payments' endpoints are the patient's own record only."""
    if user.user_type != UserType.PATIENT or account.patient.user_id != user.id:
        raise PermissionDenied("This is not your billing account.")
