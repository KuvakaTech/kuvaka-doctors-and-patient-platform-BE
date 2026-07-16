import secrets

from django.db import models as django_models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.clinics.models import (
    Clinic,
    ClinicInventoryItem,
    ClinicStaffMembership,
    Medicine,
    PermissionFlag,
    PurchaseOrder,
    PurchaseOrderStatus,
    StaffTaskGrant,
    StaffTaskGrantStatus,
)
from apps.clinics.permissions import (
    get_membership,
    has_permission,
    require_admin,
    require_membership,
    require_permission,
    validate_flag_for_role,
)
from apps.clinics.serializers import (
    ClinicInventoryItemSerializer,
    ClinicSerializer,
    ClinicStaffMembershipSerializer,
    MedicineSerializer,
    PurchaseOrderSerializer,
    StaffCreateSerializer,
    StaffTaskGrantSerializer,
)
from apps.users.models import User, UserType

# ---------------------------------------------------------------------------
# Clinic
# ---------------------------------------------------------------------------


class ClinicViewSet(viewsets.ModelViewSet):
    """
    Hospital/clinic onboarding. Anyone on the doctor-side platform can
    register a clinic; doing so makes them its owner and CLINIC_ADMIN.
    """

    queryset = Clinic.objects.filter(deleted=False)
    serializer_class = ClinicSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "head", "options"]
    lookup_field = "external_id"

    def get_queryset(self):
        return self.queryset.filter(
            staff_memberships__user=self.request.user, staff_memberships__is_active=True
        ).distinct()

    def list(self, request, *args, **kwargs):
        """Paginated clinic list, plus a `summary` block totalling across every clinic returned."""
        response = super().list(request, *args, **kwargs)

        from apps.patients.models import PatientClinicRegistration, PatientClinicStatus

        clinic_ids = list(self.get_queryset().values_list("id", flat=True))
        registrations = PatientClinicRegistration.objects.filter(
            clinic_id__in=clinic_ids, deleted=False
        )

        summary = {
            "total_clinics": len(set(clinic_ids)),
            "total_patients": registrations.values("patient_id").distinct().count(),
            "unstable_patient_count": registrations.filter(
                status__in=[PatientClinicStatus.CRITICAL, PatientClinicStatus.MONITORING]
            )
            .values("patient_id")
            .distinct()
            .count(),
        }

        # VIEW_REVENUE gating: the key is omitted, not a
        # 403, for staff without it — the rest of the summary is
        # legitimately theirs. Revenue is summed only over the subset of
        # clinics they're actually privileged at, never leaked from the
        # others just because they happen to share this response.
        privileged_clinic_ids = [
            cid
            for cid in clinic_ids
            if has_permission(request.user, cid, PermissionFlag.VIEW_REVENUE)
        ]
        if privileged_clinic_ids:
            from apps.finance.services import clinic_total_revenue

            summary["total_revenue"] = clinic_total_revenue(privileged_clinic_ids)

        response.data = {"summary": summary, **response.data}
        return response

    def perform_create(self, serializer):
        if self.request.user.user_type == UserType.PATIENT:
            raise PermissionDenied("Patient accounts cannot register a clinic.")
        clinic = serializer.save(owner=self.request.user)
        ClinicStaffMembership.objects.create(
            clinic=clinic,
            user=self.request.user,
            role=UserType.CLINIC_ADMIN,
            created_by=self.request.user,
        )
        # Local import — apps.finance depends on apps.clinics, so importing
        # it back at module load time here would be circular (same pattern
        # as the apps.clinical import elsewhere in this module).
        from apps.finance.models import BusinessOwnership, BusinessUnit, BusinessUnitType

        BusinessUnit.objects.create(
            owner=self.request.user,
            clinic=clinic,
            name=clinic.name,
            unit_type=BusinessUnitType.CLINIC,
            ownership=BusinessOwnership.OWNED,
        )

    def perform_update(self, serializer):
        require_admin(self.request.user, serializer.instance)
        serializer.save()


class DashboardSummaryView(APIView):
    """
    Totals across every clinic the caller is active staff at — the
    top-of-dashboard numbers (total clinics, total patients, patients
    needing attention, visits today, this month's revenue).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.clinical.models import Visit
        from apps.patients.models import PatientClinicRegistration, PatientClinicStatus

        clinic_ids = list(
            ClinicStaffMembership.objects.filter(
                user=request.user, is_active=True, deleted=False
            ).values_list("clinic_id", flat=True)
        )

        registrations = PatientClinicRegistration.objects.filter(
            clinic_id__in=clinic_ids, deleted=False
        )
        now = timezone.localdate()
        visits = Visit.objects.filter(clinic_id__in=clinic_ids, deleted=False)

        data = {
            "total_clinics": len(set(clinic_ids)),
            "total_patients": registrations.values("patient_id").distinct().count(),
            "needing_attention": registrations.filter(status=PatientClinicStatus.CRITICAL)
            .values("patient_id")
            .distinct()
            .count(),
            "active_visits_today": visits.filter(visit_date=now).count(),
        }

        # VIEW_REVENUE gating — see the matching note in ClinicViewSet.list().
        privileged_clinic_ids = [
            cid
            for cid in clinic_ids
            if has_permission(request.user, cid, PermissionFlag.VIEW_REVENUE)
        ]
        if privileged_clinic_ids:
            from apps.finance.services import clinic_monthly_revenue

            data["monthly_revenue"] = clinic_monthly_revenue(
                privileged_clinic_ids, year=now.year, month=now.month
            )

        return Response(data)


def _get_clinic(external_id) -> Clinic:
    return get_object_or_404(Clinic, external_id=external_id, deleted=False)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------


class ClinicStaffListCreateView(generics.ListCreateAPIView):
    """
    List staff at a clinic, or create a brand-new staff account with a role
    and permissions. Only an admin-role member (clinic_admin/doctor) or
    someone holding the `manage_staff` permission flag can create staff.
    """

    serializer_class = ClinicStaffMembershipSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_queryset(self):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        return ClinicStaffMembership.objects.filter(clinic=clinic, deleted=False)

    def create(self, request, *args, **kwargs):
        clinic = self.get_clinic()
        require_permission(request.user, clinic, PermissionFlag.MANAGE_STAFF)

        serializer = StaffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        temp_password = secrets.token_urlsafe(9)
        user = User.objects.create_user(
            email=data["email"],
            phone_number=data.get("phone_number") or None,
            password=temp_password,
            full_name=data.get("full_name", ""),
            user_type=data["role"],
        )
        # Admin-created accounts are pre-verified — an existing staff member
        # vouches for them, so there's no email-ownership loop to close.
        user.email_verified = True
        user.save(update_fields=["email_verified"])

        if data["role"] == UserType.DOCTOR:
            # The doctor self-registration flow (DoctorRegisterSerializer)
            # always creates this alongside the User; an admin creating a
            # doctor account here must do the same, or that doctor has no
            # home for preferred_medicines / their prescribing formulary.
            from apps.doctors.models import DoctorProfile

            DoctorProfile.objects.create(user=user)

        membership = ClinicStaffMembership.objects.create(
            clinic=clinic,
            user=user,
            role=data["role"],
            permissions=data.get("permissions", []),
            created_by=request.user,
        )

        return Response(
            {
                "membership": ClinicStaffMembershipSerializer(membership).data,
                "temporary_password": temp_password,
                "detail": "Share this password with the staff member out of band. "
                "They should change it on first login.",
            },
            status=status.HTTP_201_CREATED,
        )


class ClinicStaffDetailView(generics.RetrieveUpdateAPIView):
    """Update a staff member's role/permissions/active status."""

    serializer_class = ClinicStaffMembershipSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"
    lookup_url_kwarg = "membership_external_id"

    def get_queryset(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        require_permission(self.request.user, clinic, PermissionFlag.MANAGE_STAFF)
        return ClinicStaffMembership.objects.filter(clinic=clinic, deleted=False)


# ---------------------------------------------------------------------------
# Staff task delegation
# ---------------------------------------------------------------------------


class StaffTaskGrantListCreateView(generics.ListCreateAPIView):
    """
    A doctor delegating a specific task (e.g. upload_reports) to a staff
    member — separate from patient consent, this is internal to the clinic.
    """

    serializer_class = StaffTaskGrantSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_queryset(self):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        qs = StaffTaskGrant.objects.filter(clinic=clinic, deleted=False)
        if self.request.query_params.get("mine") == "true":
            qs = qs.filter(grantee=self.request.user)
        return qs

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        # Task delegation is a function of being a doctor by profession
        # (User.user_type), not of the administrative role this particular
        # membership carries — a doctor who registered the clinic holds a
        # clinic_admin membership there but must still be able to delegate.
        if self.request.user.user_type != UserType.DOCTOR:
            raise PermissionDenied("Only a doctor can delegate a task to a staff member.")

        grantee = serializer.validated_data["grantee"]
        grantee_membership = get_membership(grantee, clinic)
        if grantee_membership is None:
            raise ValidationError({"grantee": "This user is not an active staff member here."})
        # e.g. add_vitals can only ever be delegated to a nurse — reject
        # before it's ever stored, not just at enforcement time.
        validate_flag_for_role(serializer.validated_data["task_type"], grantee_membership.role)

        serializer.save(clinic=clinic, granted_by=self.request.user)


class StaffTaskGrantRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        grant = get_object_or_404(
            StaffTaskGrant, clinic=clinic, external_id=external_id, deleted=False
        )
        if grant.granted_by_id != request.user.id:
            require_admin(request.user, clinic)

        grant.status = StaffTaskGrantStatus.REVOKED
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["status", "revoked_at"])
        return Response(StaffTaskGrantSerializer(grant).data)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _is_staff_at_owner_clinics(user, owner_id) -> bool:
    """Is `user` an active staff member at any clinic owned by `owner_id`?"""
    return ClinicStaffMembership.objects.filter(
        user=user, is_active=True, clinic__owner_id=owner_id, clinic__deleted=False
    ).exists()


class MedicineListCreateView(generics.ListCreateAPIView):
    """
    A doctor's own medicine catalog — shared across every clinic that
    doctor owns, not visible to other doctors' clinics.

    A doctor account can call this with no params at all (owner is just
    `request.user`). Staff accounts (nurse/receptionist/etc.) don't have
    their own catalog, so they must pass `?clinic=<external_id>` to work
    within the catalog of the doctor who owns that clinic.
    """

    serializer_class = MedicineSerializer
    permission_classes = [IsAuthenticated]

    def _get_owner_id(self):
        if self.request.user.user_type == UserType.DOCTOR:
            return self.request.user.id
        clinic_id = self.request.query_params.get("clinic") or self.request.data.get("clinic")
        if not clinic_id:
            raise ValidationError(
                {
                    "clinic": "Required for non-doctor accounts — pass ?clinic=<external_id> (or in the body for POST)."
                }
            )
        clinic = get_object_or_404(Clinic, external_id=clinic_id, deleted=False)
        require_membership(self.request.user, clinic)
        return clinic.owner_id

    def get_queryset(self):
        owner_id = self._get_owner_id()
        qs = Medicine.objects.filter(deleted=False, owner_id=owner_id)
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                django_models.Q(name__icontains=search)
                | django_models.Q(generic_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        owner_id = self._get_owner_id()
        serializer.save(owner_id=owner_id)


class MedicineDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    View/edit/remove one entry in a doctor's medicine catalog. Restricted
    to staff at a clinic owned by the same doctor who owns the entry —
    other doctors' clinics can't see or touch it.
    """

    queryset = Medicine.objects.filter(deleted=False)
    serializer_class = MedicineSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if request.user.user_type == UserType.PATIENT or not _is_staff_at_owner_clinics(
            request.user, obj.owner_id
        ):
            self.permission_denied(
                request, message="You don't have access to this clinic's medicine catalog."
            )

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


class ClinicInventoryListCreateView(generics.ListCreateAPIView):
    """
    A clinic's own stock. Also doubles as the "search medicine to
    prescribe" lookup for the visit/prescription flow — pass
    ?search=<text> to match against the medicine's name or generic name,
    scoped to this clinic's own stock only.
    """

    serializer_class = ClinicInventoryItemSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["clinic"] = self.get_clinic()
        return context

    def get_queryset(self):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        qs = ClinicInventoryItem.objects.filter(clinic=clinic, deleted=False)
        if self.request.query_params.get("low_stock") == "true":
            qs = qs.filter(quantity_in_stock__lt=django_models.F("reorder_threshold"))
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                django_models.Q(medicine__name__icontains=search)
                | django_models.Q(medicine__generic_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        require_permission(self.request.user, clinic, PermissionFlag.MANAGE_INVENTORY)
        serializer.save(clinic=clinic)


class ClinicInventoryDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ClinicInventoryItemSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["clinic"] = _get_clinic(self.kwargs["clinic_external_id"])
        return context

    def get_queryset(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        require_permission(self.request.user, clinic, PermissionFlag.MANAGE_INVENTORY)
        return ClinicInventoryItem.objects.filter(clinic=clinic, deleted=False)

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


class PurchaseOrderListCreateView(generics.ListCreateAPIView):
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_queryset(self):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        return PurchaseOrder.objects.filter(clinic=clinic, deleted=False)

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        require_permission(self.request.user, clinic, PermissionFlag.MANAGE_INVENTORY)
        serializer.save(
            clinic=clinic,
            status=PurchaseOrderStatus.ORDERED,
            ordered_by=self.request.user,
            ordered_at=timezone.now(),
        )


class PurchaseOrderReceiveView(APIView):
    """Mark a purchase order received and add its items into clinic stock."""

    permission_classes = [IsAuthenticated]

    def post(self, request, clinic_external_id, external_id):
        clinic = _get_clinic(clinic_external_id)
        require_permission(request.user, clinic, PermissionFlag.MANAGE_INVENTORY)
        order = get_object_or_404(
            PurchaseOrder, clinic=clinic, external_id=external_id, deleted=False
        )
        if order.status != PurchaseOrderStatus.ORDERED:
            raise ValidationError({"status": "Only an ordered purchase order can be received."})

        for line in order.items:
            medicine = get_object_or_404(Medicine, external_id=line["medicine_id"], deleted=False)
            item, _ = ClinicInventoryItem.objects.get_or_create(
                clinic=clinic,
                medicine=medicine,
                defaults={"unit_price": line.get("unit_price")},
            )
            ClinicInventoryItem.objects.filter(pk=item.pk).update(
                quantity_in_stock=django_models.F("quantity_in_stock") + line["quantity"]
            )

        order.status = PurchaseOrderStatus.RECEIVED
        order.received_at = timezone.now()
        order.save(update_fields=["status", "received_at"])

        # Local import — apps.clinics is a shared base app apps.finance
        # already depends on directly (RevenueEntry.clinic, .purchase_order),
        # but importing apps.finance back here at module load time would be
        # circular the other way.
        from apps.finance.services import record_purchase_expense

        record_purchase_expense(order, request=request)
        return Response(PurchaseOrderSerializer(order).data)
