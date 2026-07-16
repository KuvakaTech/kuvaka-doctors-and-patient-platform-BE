"""
Clinic-scoped authorization helpers.

Role/permission checks here are per-clinic (a user's `ClinicStaffMembership`
row), not global — the same user can be a CLINIC_ADMIN at one clinic and a
plain NURSE at another. There's no DRF `BasePermission` subclass because
every check needs the target clinic, which isn't known until the view
resolves it (from a URL kwarg or the request body) — these are called
directly from view code instead.

`require_permission()` is the one real enforcement path: a capability is
granted if the caller's role bypasses checks entirely (clinic_admin/doctor),
or their membership carries the flag as a standing permission, or — for
patient-scoped actions — a doctor has delegated it to them via an active
`StaffTaskGrant` for that specific patient. Every flag also has a fixed set
of roles eligible to hold it at all (`PERMISSION_ROLE_MAP`), validated
wherever `permissions` or a task grant's `task_type` is written — see
`apps.clinics.serializers`.
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.clinics.models import PERMISSION_ROLE_MAP, ClinicStaffMembership, StaffTaskGrantStatus
from apps.users.models import UserType

# Roles that can do anything at their clinic by default, without needing an
# explicit permission flag or task grant.
ADMIN_ROLES = {UserType.CLINIC_ADMIN, UserType.DOCTOR}


def get_membership(user, clinic) -> ClinicStaffMembership | None:
    return ClinicStaffMembership.objects.filter(
        clinic=clinic, user=user, is_active=True, deleted=False
    ).first()


def require_membership(user, clinic) -> ClinicStaffMembership:
    membership = get_membership(user, clinic)
    if membership is None:
        raise PermissionDenied("You are not an active staff member of this clinic.")
    return membership


def require_admin(user, clinic) -> ClinicStaffMembership:
    """Require the caller to hold an admin-by-default role at `clinic` (clinic_admin/doctor)."""
    membership = require_membership(user, clinic)
    if membership.role not in ADMIN_ROLES:
        raise PermissionDenied("Only a clinic admin or doctor can perform this action.")
    return membership


def validate_flag_for_role(flag: str, role: str) -> None:
    """
    Raise if `flag` may not be held by `role`. Admin-default roles
    (clinic_admin/doctor) may hold any flag; every other role is restricted
    to `PERMISSION_ROLE_MAP[flag]`. Call this wherever a flag is about to be
    written (staff permissions, task grants) — never only at check time.
    """
    if role in ADMIN_ROLES:
        return
    allowed_roles = PERMISSION_ROLE_MAP.get(flag, set())
    if role not in allowed_roles:
        raise ValidationError(
            f"The '{flag}' permission cannot be granted to role '{role}'."
        )


def _has_active_task_grant(user, clinic, flag: str, patient) -> bool:
    from apps.clinics.models import StaffTaskGrant

    now = timezone.now()
    qs = StaffTaskGrant.objects.filter(
        clinic=clinic,
        grantee=user,
        task_type=flag,
        status=StaffTaskGrantStatus.ACTIVE,
        deleted=False,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if patient is not None:
        qs = qs.filter(Q(patient__isnull=True) | Q(patient=patient))
    else:
        qs = qs.filter(patient__isnull=True)
    return qs.exists()


def has_permission(user, clinic, flag: str, *, patient=None) -> bool:
    """
    Non-raising variant of require_permission — for conditionally
    including/omitting data (e.g. revenue figures gated on VIEW_REVENUE,
    see the four call sites in apps.clinics.views/apps.patients.views)
    rather than blocking an entire request over one field. Returns False
    for a non-member instead of raising, unlike require_membership.
    """
    membership = get_membership(user, clinic)
    if membership is None:
        return False
    if membership.role in ADMIN_ROLES:
        return True
    if flag in membership.permissions:
        return True
    return _has_active_task_grant(user, clinic, flag, patient)


def require_permission(user, clinic, flag: str, *, patient=None) -> ClinicStaffMembership:
    """
    Require the caller to hold `flag` at `clinic` — via admin-default role,
    a standing permission on their membership, or (when `patient` is given)
    an active task grant delegated to them for that patient.
    """
    membership = require_membership(user, clinic)
    if has_permission(user, clinic, flag, patient=patient):
        return membership
    raise PermissionDenied("You do not have permission to perform this action.")


def is_doctor_membership(membership: ClinicStaffMembership) -> bool:
    return membership.role == UserType.DOCTOR
