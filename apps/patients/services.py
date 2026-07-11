"""Cross-cutting patient-profile operations that don't belong to a single model."""

from django.db import transaction

from apps.patients.models import (
    ConsentGrant,
    FamilyMember,
    PatientClinicRegistration,
    PatientMergeLog,
    PatientProfile,
)


@transaction.atomic
def merge_patients(*, primary: PatientProfile, duplicate: PatientProfile, merged_by, reason: str) -> PatientMergeLog:
    """
    Fold `duplicate` into `primary`: every clinic registration, consent
    grant, and family link owned by the duplicate is reassigned to the
    primary, then the duplicate account is deactivated and soft-deleted.

    Rows that would violate a unique constraint after reassignment (e.g. the
    primary is already registered at a clinic the duplicate was also
    registered at) are dropped rather than reassigned — the primary's own
    row already covers that relationship.
    """
    if primary.pk == duplicate.pk:
        raise ValueError("Cannot merge a patient into itself.")

    existing_clinics = set(
        PatientClinicRegistration.objects.filter(patient=primary).values_list(
            "clinic_id", flat=True
        )
    )
    for reg in PatientClinicRegistration.objects.filter(patient=duplicate):
        if reg.clinic_id in existing_clinics:
            reg.delete()
        else:
            reg.patient = primary
            reg.save(update_fields=["patient"])

    ConsentGrant.objects.filter(patient=duplicate).update(patient=primary)

    existing_related = set(
        FamilyMember.objects.filter(patient=primary).values_list("related_patient_id", flat=True)
    )
    for link in FamilyMember.objects.filter(patient=duplicate):
        if link.related_patient_id == primary.pk or link.related_patient_id in existing_related:
            link.delete()
        else:
            link.patient = primary
            link.save(update_fields=["patient"])

    duplicate.deleted = True
    duplicate.save(update_fields=["deleted"])
    duplicate.user.is_active = False
    duplicate.user.save(update_fields=["is_active"])

    return PatientMergeLog.objects.create(
        primary_patient=primary,
        merged_patient=duplicate,
        merged_by=merged_by,
        reason=reason,
    )
