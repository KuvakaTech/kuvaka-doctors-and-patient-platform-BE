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
    PurchaseOrder,
    PurchaseOrderStatus,
    StaffTaskGrant,
    StaffTaskGrantStatus,
)
from apps.clinics.permissions import get_membership, require_admin, require_membership
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

    def perform_update(self, serializer):
        require_admin(self.request.user, serializer.instance)
        serializer.save()


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
        require_admin(request.user, clinic, permission_flag="manage_staff")

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
        require_admin(self.request.user, clinic, permission_flag="manage_staff")
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
        if get_membership(grantee, clinic) is None:
            raise ValidationError({"grantee": "This user is not an active staff member here."})

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


class MedicineListCreateView(generics.ListCreateAPIView):
    """Global medicine catalog — shared across every clinic, not clinic-scoped."""

    queryset = Medicine.objects.filter(deleted=False)
    serializer_class = MedicineSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                django_models.Q(name__icontains=search)
                | django_models.Q(generic_name__icontains=search)
            )
        return qs


class ClinicInventoryListCreateView(generics.ListCreateAPIView):
    serializer_class = ClinicInventoryItemSerializer
    permission_classes = [IsAuthenticated]

    def get_clinic(self):
        return _get_clinic(self.kwargs["clinic_external_id"])

    def get_queryset(self):
        clinic = self.get_clinic()
        require_membership(self.request.user, clinic)
        qs = ClinicInventoryItem.objects.filter(clinic=clinic, deleted=False)
        if self.request.query_params.get("low_stock") == "true":
            qs = qs.filter(quantity_in_stock__lt=django_models.F("reorder_threshold"))
        return qs

    def perform_create(self, serializer):
        clinic = self.get_clinic()
        require_admin(self.request.user, clinic, permission_flag="manage_inventory")
        serializer.save(clinic=clinic)


class ClinicInventoryDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = ClinicInventoryItemSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        clinic = _get_clinic(self.kwargs["clinic_external_id"])
        require_admin(self.request.user, clinic, permission_flag="manage_inventory")
        return ClinicInventoryItem.objects.filter(clinic=clinic, deleted=False)


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
        require_admin(self.request.user, clinic, permission_flag="manage_inventory")
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
        require_admin(request.user, clinic, permission_flag="manage_inventory")
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
        return Response(PurchaseOrderSerializer(order).data)
