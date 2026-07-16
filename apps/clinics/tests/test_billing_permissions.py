import pytest
from rest_framework.exceptions import ValidationError

from apps.clinics.models import PermissionFlag
from apps.clinics.permissions import validate_flag_for_role
from apps.users.models import UserType


@pytest.mark.parametrize(
    "role",
    [UserType.NURSE, UserType.RECEPTIONIST, UserType.PHARMACIST, UserType.LAB_TECHNICIAN],
)
@pytest.mark.parametrize("flag", [PermissionFlag.MANAGE_BILLING, PermissionFlag.MANAGE_REFUNDS])
def test_billing_flags_grantable_to_every_staff_role(role, flag):
    # Billing permissions are owner-decided, not role-restricted —
    # every non-admin staff role is *eligible*; it's up to the owner/doctor
    # whether a given person actually holds the flag.
    validate_flag_for_role(flag, role)  # must not raise


@pytest.mark.parametrize("flag", [PermissionFlag.MANAGE_BILLING, PermissionFlag.MANAGE_REFUNDS])
def test_billing_flags_still_rejected_for_admin_default_roles_map_bypass(flag):
    # CLINIC_ADMIN/DOCTOR bypass PERMISSION_ROLE_MAP entirely (see
    # ADMIN_ROLES in apps.clinics.permissions) — this just confirms the new
    # flags don't special-case that bypass away.
    validate_flag_for_role(flag, UserType.CLINIC_ADMIN)
    validate_flag_for_role(flag, UserType.DOCTOR)


def test_manage_billing_still_rejects_patient_role():
    with pytest.raises(ValidationError):
        validate_flag_for_role(PermissionFlag.MANAGE_BILLING, UserType.PATIENT)
