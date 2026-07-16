"""
Access control for clinical records — composes the two checks already built
elsewhere rather than inventing a third system:
  - `apps.clinics.permissions.require_membership` — is the caller staff at
    this clinic at all.
  - `apps.patients.permissions.has_consent` — did the patient (directly, or
    via registering at this clinic — see `apps.patients.services.
    ensure_clinic_consent`) actually consent to this clinic seeing their
    data.
Both must hold for any clinical read/write.
"""

from rest_framework.exceptions import PermissionDenied

from apps.clinics.permissions import require_membership
from apps.patients.models import ConsentScope
from apps.patients.permissions import has_consent


def require_patient_access(user, clinic, patient, scope_item: str = ConsentScope.FULL):
    """Require the caller to be staff at `clinic` AND hold consent covering `scope_item`."""
    membership = require_membership(user, clinic)
    if not has_consent(patient, user, scope_item):
        raise PermissionDenied(
            "No active consent grant covers this patient for your clinic."
        )
    return membership
