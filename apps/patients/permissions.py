"""
Patient-consent authorization helper.

Mirrors `apps.clinics.permissions.require_permission` on the other side of
the relationship: that module asks "can this staff member perform this
action at their clinic", this one asks "did this patient agree to let this
requester see this category of their data". Nothing calls `has_consent` yet
— no clinical-data endpoint exists to gate — but the check is real and
ready: once a Diagnosis/Vitals/Report endpoint is built, it filters
cross-clinic reads through this rather than inventing its own scope check.
"""

from django.db.models import Q
from django.utils import timezone

from apps.patients.models import ConsentGrant, ConsentGrantStatus, ConsentScope


def has_consent(patient, requester_user, scope_item: str) -> bool:
    """
    True if `requester_user` currently holds an active, unexpired consent
    grant from `patient` covering `scope_item` — either granted to them
    directly, or to a clinic they're an active staff member of.
    """
    from apps.clinics.models import ClinicStaffMembership

    now = timezone.now()
    clinic_ids = ClinicStaffMembership.objects.filter(
        user=requester_user, is_active=True, deleted=False
    ).values_list("clinic_id", flat=True)

    grants = ConsentGrant.objects.filter(
        patient=patient, status=ConsentGrantStatus.ACTIVE, deleted=False
    ).filter(Q(grantee_user=requester_user) | Q(grantee_clinic_id__in=clinic_ids))
    grants = grants.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

    return any(scope_item in grant.scope or ConsentScope.FULL in grant.scope for grant in grants)
