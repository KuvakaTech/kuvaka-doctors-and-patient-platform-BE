"""
Clinic-scoped authorization helpers.

Role/permission checks here are per-clinic (a user's `ClinicStaffMembership`
row), not global — the same user can be a CLINIC_ADMIN at one clinic and a
plain NURSE at another. There's no DRF `BasePermission` subclass because
every check needs the target clinic, which isn't known until the view
resolves it (from a URL kwarg or the request body) — these are called
directly from view code instead.
"""

from rest_framework.exceptions import PermissionDenied

from apps.clinics.models import ClinicStaffMembership
from apps.users.models import UserType

# Roles that can manage staff/permissions/inventory by default, without
# needing an explicit permission flag.
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


def require_admin(user, clinic, *, permission_flag: str | None = None) -> ClinicStaffMembership:
    """
    Require the caller to be able to administer `clinic` — either holding an
    admin-by-default role (clinic_admin/doctor) or the given permission flag
    explicitly granted on their membership.
    """
    membership = require_membership(user, clinic)
    if membership.role in ADMIN_ROLES:
        return membership
    if permission_flag and permission_flag in membership.permissions:
        return membership
    raise PermissionDenied("You do not have permission to perform this action.")


def is_doctor_membership(membership: ClinicStaffMembership) -> bool:
    return membership.role == UserType.DOCTOR
