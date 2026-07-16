"""
Doctor-controlled access to finance data.

Mirrors apps.patients.permissions.has_consent / apps.clinics.permissions.
require_permission on the same relationship shape: a doctor's finance data
is closed by default, and only visible to someone else via an explicit,
revocable FinanceAccessGrant. There
is deliberately no clinic-membership/VIEW_REVENUE path here — clinic roles
grant nothing on finance endpoints (that was the v1 design and was
explicitly dropped in review).
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.finance.models import FinanceAccessGrant, FinanceGrantStatus
from apps.users.models import User, UserType


def resolve_finance_viewer(request_user, doctor_external_id: str | None):
    """
    Resolve whose finance data is being viewed.

    Returns (doctor, grant): `doctor` is the User whose data is in scope;
    `grant` is the active FinanceAccessGrant if access is via a grant, or
    None if the caller is viewing their own data.

    Raises PermissionDenied if the caller has no valid path to the data.
    """
    if not doctor_external_id:
        if request_user.user_type != UserType.DOCTOR:
            raise PermissionDenied("Only a doctor account has finance data of their own.")
        return request_user, None

    doctor = get_object_or_404(
        User, external_id=doctor_external_id, user_type=UserType.DOCTOR, deleted=False
    )
    if doctor.pk == request_user.pk:
        return doctor, None

    now = timezone.now()
    grant = (
        FinanceAccessGrant.objects.filter(
            doctor=doctor,
            grantee=request_user,
            status=FinanceGrantStatus.ACTIVE,
            deleted=False,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .first()
    )
    if grant is None:
        raise PermissionDenied("You do not have access to this doctor's finance data.")
    return doctor, grant


def require_clinic_owner(user, clinic) -> None:
    """
    Revenue-share rules are managed at the OWNER level specifically
    (`Clinic.owner`), not by admin-role staff generally — a clinic can
    have CLINIC_ADMIN staff who aren't its registered owner (created via
    apps.clinics.views.ClinicStaffListCreateView), and money-sharing
    agreements are the owner's call, not theirs.
    """
    if clinic.owner_id != user.pk:
        raise PermissionDenied("Only the clinic owner can manage revenue share rules.")


def scope_queryset_to_grant(
    queryset,
    grant: FinanceAccessGrant | None,
    *,
    clinic_field="clinic",
    business_unit_field="business_unit",
):
    """
    Narrow `queryset` to whatever scope `grant` carries. A grant with no
    clinic/business_unit set covers the doctor's entire finance picture —
    no additional filtering. Pass `grant=None` (own-data access) through
    unchanged.
    """
    if grant is None:
        return queryset
    if grant.clinic_id is not None:
        return queryset.filter(**{clinic_field: grant.clinic_id})
    if grant.business_unit_id is not None:
        return queryset.filter(**{business_unit_field: grant.business_unit_id})
    return queryset
