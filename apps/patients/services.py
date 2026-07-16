"""Cross-cutting patient-profile operations that don't belong to a single model."""

from django.db import transaction
from django.utils import timezone

from apps.patients.models import (
    ConsentGrant,
    ConsentGrantStatus,
    ConsentScope,
    FamilyMember,
    PatientClinicRegistration,
    PatientMergeLog,
    PatientProfile,
)


def ensure_clinic_consent(*, patient: PatientProfile, clinic, granted_by) -> ConsentGrant:
    """
    Registering a patient at a clinic is itself the explicit consent event —
    someone (the patient via OTP, or staff with the patient physically
    present) took a real action to associate them with this specific
    clinic. Rather than leaving that consent implicit, this records it as a
    real, auditable, revocable `ConsentGrant` scoped to just this clinic —
    satisfying "closed by default, explicit grant required" without a
    separate approval round-trip for a clinic the patient just registered
    at. Cross-clinic access still requires its own grant.

    Idempotent — safe to call on every registration; reuses an existing
    active grant for this (patient, clinic) pair instead of stacking dupes.
    """
    existing = ConsentGrant.objects.filter(
        patient=patient,
        grantee_clinic=clinic,
        status=ConsentGrantStatus.ACTIVE,
        deleted=False,
    ).first()
    if existing is not None:
        return existing

    return ConsentGrant.objects.create(
        patient=patient,
        grantee_clinic=clinic,
        scope=[ConsentScope.FULL],
        status=ConsentGrantStatus.ACTIVE,
        granted_at=timezone.now(),
        requested_by=granted_by,
    )


def _reassign_billing_accounts(*, primary: PatientProfile, duplicate: PatientProfile) -> None:
    """
    Re-point the duplicate's billing history onto the primary, for the
    same reason RevenueEntry gets reassigned below: a merge must never
    strand live money on a soft-deleted, inaccessible record.

    Local import — apps.patients loads before apps.billing in LOCAL_APPS,
    so this can only ever be a function-local import, matching the
    circular-import-avoidance convention used everywhere else in this
    codebase (e.g. apps.clinical.views importing apps.billing/apps.finance).
    """
    from apps.billing.models import ChargeItem, Invoice, PatientAccount, Payment
    from apps.billing.services import recalculate_account
    from apps.core.money import quantize2

    primary_clinic_ids = set(
        PatientAccount.objects.filter(patient=primary, deleted=False).values_list(
            "clinic_id", flat=True
        )
    )
    for dup_account in PatientAccount.objects.filter(patient=duplicate, deleted=False):
        if dup_account.clinic_id not in primary_clinic_ids:
            # No account collision at this clinic — the whole account (and
            # its already-correct rollups) simply changes owner; nothing
            # underneath it needs moving.
            dup_account.patient = primary
            dup_account.save(update_fields=["patient"])
            ChargeItem.objects.filter(account=dup_account).update(patient=primary)
            Invoice.objects.filter(account=dup_account).update(patient=primary)
            continue

        # The primary already has an account at this clinic — re-point
        # every child row onto it rather than leaving live money on a
        # soft-deleted account, and fold the duplicate's advance balance in.
        primary_account = PatientAccount.objects.get(
            patient=primary, clinic_id=dup_account.clinic_id, deleted=False
        )
        ChargeItem.objects.filter(account=dup_account).update(
            account=primary_account, patient=primary
        )
        Invoice.objects.filter(account=dup_account).update(
            account=primary_account, patient=primary
        )
        Payment.objects.filter(account=dup_account).update(account=primary_account)
        primary_account.advance_balance = quantize2(
            primary_account.advance_balance + dup_account.advance_balance
        )
        primary_account.save(update_fields=["advance_balance"])
        dup_account.deleted = True
        dup_account.save(update_fields=["deleted"])
        recalculate_account(primary_account)


@transaction.atomic
def merge_patients(
    *, primary: PatientProfile, duplicate: PatientProfile, merged_by, reason: str
) -> PatientMergeLog:
    """
    Fold `duplicate` into `primary`: every clinic registration, consent
    grant, family link, revenue-attribution record, and billing account
    owned by the duplicate is reassigned to the primary, then the
    duplicate account is deactivated and soft-deleted.

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

    # Revenue attribution — no unique constraint on RevenueEntry.patient,
    # so this is a plain reassignment (no collision case to handle, unlike
    # billing accounts below).
    from apps.finance.models import RevenueEntry

    RevenueEntry.objects.filter(patient=duplicate).update(patient=primary)

    _reassign_billing_accounts(primary=primary, duplicate=duplicate)

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
