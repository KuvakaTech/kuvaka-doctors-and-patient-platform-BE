import datetime as dt

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.services.financial_audit import FinancialEvent, log_financial_event
from apps.finance.models import (
    BusinessUnit,
    FinanceAccessGrant,
    FinanceGrantStatus,
    RevenueEntry,
    RevenueShareRule,
)
from apps.finance.permissions import (
    require_clinic_owner,
    resolve_finance_viewer,
    scope_queryset_to_grant,
)
from apps.finance.serializers import (
    BusinessUnitSerializer,
    FinanceAccessGrantCreateSerializer,
    FinanceAccessGrantSerializer,
    RevenueEntrySerializer,
    RevenueShareRuleSerializer,
)
from apps.finance.services import compute_dashboard, create_manual_entry, update_manual_entry
from apps.users.models import User, UserType


def _require_doctor(user) -> None:
    if user.user_type != UserType.DOCTOR:
        raise PermissionDenied("This action is only available to doctor accounts.")


# ---------------------------------------------------------------------------
# Business units
# ---------------------------------------------------------------------------


class BusinessUnitListCreateView(generics.ListCreateAPIView):
    serializer_class = BusinessUnitSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        doctor, grant = resolve_finance_viewer(
            self.request.user, self.request.query_params.get("doctor")
        )
        qs = BusinessUnit.objects.filter(owner=doctor, deleted=False)
        # business_unit_field="pk" — a grant scoped to one specific unit
        # should list only that unit's own row, not filter a (nonexistent)
        # `business_unit` field on BusinessUnit itself.
        qs = scope_queryset_to_grant(qs, grant, clinic_field="clinic", business_unit_field="pk")
        for param in ("unit_type", "ownership", "is_active"):
            value = self.request.query_params.get(param)
            if value is not None:
                qs = qs.filter(**{param: value})
        return qs

    def perform_create(self, serializer):
        _require_doctor(self.request.user)
        serializer.save(owner=self.request.user)


class BusinessUnitDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BusinessUnitSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        doctor, grant = resolve_finance_viewer(
            self.request.user, self.request.query_params.get("doctor")
        )
        qs = BusinessUnit.objects.filter(owner=doctor, deleted=False)
        return scope_queryset_to_grant(qs, grant, clinic_field="clinic", business_unit_field="pk")

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if request.method in ("PUT", "PATCH", "DELETE") and obj.owner_id != request.user.id:
            self.permission_denied(request, message="Only the owning doctor can modify this.")

    def perform_update(self, serializer):
        # clinic/unit_type are the unit's identity — immutable after
        # creation so revenue history attributed to this unit keeps
        # meaning the same thing it did when it was written.
        instance = serializer.instance
        data = serializer.validated_data
        new_clinic = data["clinic"].pk if data.get("clinic") else None
        if "clinic" in data and new_clinic != instance.clinic_id:
            raise ValidationError({"clinic": "Immutable — create a new business unit instead."})
        if "unit_type" in data and data["unit_type"] != instance.unit_type:
            raise ValidationError({"unit_type": "Immutable — create a new business unit instead."})
        serializer.save()

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])


# ---------------------------------------------------------------------------
# Revenue entries
# ---------------------------------------------------------------------------


class RevenueEntryListCreateView(generics.ListCreateAPIView):
    serializer_class = RevenueEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        doctor, _grant = resolve_finance_viewer(
            self.request.user, self.request.query_params.get("doctor")
        )
        context["viewer_id"] = doctor.id
        return context

    def get_queryset(self):
        doctor, grant = resolve_finance_viewer(
            self.request.user, self.request.query_params.get("doctor")
        )
        qs = RevenueEntry.objects.filter(Q(doctor=doctor) | Q(owner=doctor), deleted=False)
        qs = scope_queryset_to_grant(qs, grant)

        params = self.request.query_params
        if params.get("from"):
            qs = qs.filter(occurred_on__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(occurred_on__lte=params["to"])
        if params.get("source_type"):
            qs = qs.filter(source_type=params["source_type"])
        if params.get("business_unit"):
            qs = qs.filter(business_unit__external_id=params["business_unit"])
        if params.get("clinic"):
            qs = qs.filter(clinic__external_id=params["clinic"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("payment_mode"):
            qs = qs.filter(payment_mode=params["payment_mode"])
        role = params.get("role")
        if role == "earned":
            qs = qs.filter(doctor=doctor)
        elif role == "owned":
            qs = qs.filter(owner=doctor)

        return qs.order_by("-occurred_on")

    def create(self, request, *args, **kwargs):
        _require_doctor(request.user)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = create_manual_entry(
            doctor=request.user, validated_data=serializer.validated_data, request=request
        )
        return Response(self.get_serializer(entry).data, status=status.HTTP_201_CREATED)


class RevenueEntryDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RevenueEntrySerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["viewer_id"] = self.request.user.id
        return context

    def get_queryset(self):
        user = self.request.user
        return RevenueEntry.objects.filter(Q(doctor=user) | Q(owner=user), deleted=False)

    def _require_manual(self, instance):
        if instance.visit_id is not None:
            raise ValidationError(
                "Visit-linked entries cannot be edited here — edit the visit instead."
            )
        if instance.charge_item_id is not None:
            raise ValidationError(
                "Billing-originated entries cannot be edited here — correct the source charge, "
                "invoice, or payment through the billing screens instead."
            )
        if instance.doctor_id != self.request.user.id:
            raise PermissionDenied("Only the recording doctor can edit this entry.")

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        self._require_manual(instance)
        partial = kwargs.pop("partial", False)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        entry = update_manual_entry(instance, serializer.validated_data, request=request)
        return Response(self.get_serializer(entry).data)

    def perform_destroy(self, instance):
        self._require_manual(instance)
        instance.deleted = True
        instance.save(update_fields=["deleted"])
        log_financial_event(
            self.request,
            FinancialEvent.ENTRY_CANCELLED,
            actor=self.request.user,
            object_type="revenue_entry",
            object_id=str(instance.external_id),
            clinic=instance.clinic,
        )


# ---------------------------------------------------------------------------
# Revenue share rules
# ---------------------------------------------------------------------------


class RevenueShareRuleListCreateView(generics.ListCreateAPIView):
    serializer_class = RevenueShareRuleSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from apps.clinics.models import Clinic

        clinic_id = self.request.query_params.get("clinic")
        if not clinic_id:
            raise ValidationError({"clinic": "Required — pass ?clinic=<external_id>."})
        clinic = get_object_or_404(Clinic, external_id=clinic_id, deleted=False)

        if clinic.owner_id == self.request.user.id:
            return RevenueShareRule.objects.filter(clinic=clinic, deleted=False)
        # Not the owner — a doctor may see only their own rule at this clinic.
        return RevenueShareRule.objects.filter(
            clinic=clinic, doctor=self.request.user, deleted=False
        )

    def perform_create(self, serializer):
        clinic = serializer.validated_data["clinic"]
        require_clinic_owner(self.request.user, clinic)
        doctor = serializer.validated_data["doctor"]
        from apps.clinics.permissions import get_membership

        if get_membership(doctor, clinic) is None:
            raise ValidationError({"doctor": "This user is not an active staff member here."})
        rule = serializer.save(created_by=self.request.user)
        log_financial_event(
            self.request,
            FinancialEvent.SHARE_RULE_CREATED,
            actor=self.request.user,
            object_type="revenue_share_rule",
            object_id=str(rule.external_id),
            clinic=clinic,
        )


class RevenueShareRuleDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = RevenueShareRuleSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "external_id"

    def get_queryset(self):
        return RevenueShareRule.objects.filter(deleted=False)

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if request.method in ("PUT", "PATCH", "DELETE"):
            require_clinic_owner(request.user, obj.clinic)
        elif obj.clinic.owner_id != request.user.id and obj.doctor_id != request.user.id:
            self.permission_denied(request, message="You cannot view this rule.")

    def perform_update(self, serializer):
        # clinic/doctor are the rule's identity, set at creation — never
        # rewritten. Ownership was checked against the CURRENT clinic
        # (check_object_permissions above), so allowing a new clinic here
        # would let an owner move a rule onto a clinic they don't own.
        instance = serializer.instance
        data = serializer.validated_data
        if "clinic" in data and data["clinic"].pk != instance.clinic_id:
            raise ValidationError({"clinic": "Immutable — create a new rule instead."})
        if "doctor" in data and data["doctor"].pk != instance.doctor_id:
            raise ValidationError({"doctor": "Immutable — create a new rule instead."})
        rule = serializer.save()
        log_financial_event(
            self.request,
            FinancialEvent.SHARE_RULE_UPDATED,
            actor=self.request.user,
            object_type="revenue_share_rule",
            object_id=str(rule.external_id),
            clinic=rule.clinic,
        )

    def perform_destroy(self, instance):
        instance.deleted = True
        instance.save(update_fields=["deleted"])
        log_financial_event(
            self.request,
            FinancialEvent.SHARE_RULE_UPDATED,
            actor=self.request.user,
            object_type="revenue_share_rule",
            object_id=str(instance.external_id),
            clinic=instance.clinic,
            metadata={"action": "deleted"},
        )


# ---------------------------------------------------------------------------
# Finance access grants
# ---------------------------------------------------------------------------


class FinanceAccessGrantListCreateView(generics.ListAPIView):
    serializer_class = FinanceAccessGrantSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if self.request.query_params.get("mine") == "true":
            return FinanceAccessGrant.objects.filter(grantee=user, deleted=False)
        _require_doctor(user)
        return FinanceAccessGrant.objects.filter(doctor=user, deleted=False)

    def post(self, request, *args, **kwargs):
        _require_doctor(request.user)
        serializer = FinanceAccessGrantCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lookup = {"deleted": False}
        if data.get("grantee_email"):
            lookup["email"] = data["grantee_email"].lower()
        else:
            lookup["phone_number"] = data["grantee_phone_number"]
        grantee = User.objects.filter(**lookup).first()
        if grantee is None:
            raise ValidationError({"grantee": "No matching platform account found."})
        if grantee.id == request.user.id:
            raise ValidationError({"grantee": "Cannot grant access to yourself."})

        grant = FinanceAccessGrant.objects.create(
            doctor=request.user,
            grantee=grantee,
            clinic=data.get("clinic"),
            business_unit=data.get("business_unit"),
            expires_at=data.get("expires_at"),
            notes=data.get("notes", ""),
        )
        log_financial_event(
            request,
            FinancialEvent.GRANT_CREATED,
            actor=request.user,
            object_type="finance_access_grant",
            object_id=str(grant.external_id),
            clinic=grant.clinic,
        )
        return Response(FinanceAccessGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


class FinanceAccessGrantRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, external_id):
        grant = get_object_or_404(
            FinanceAccessGrant, external_id=external_id, doctor=request.user, deleted=False
        )
        if grant.status != FinanceGrantStatus.ACTIVE:
            raise ValidationError({"status": "Only an active grant can be revoked."})
        grant.status = FinanceGrantStatus.REVOKED
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["status", "revoked_at"])
        log_financial_event(
            request,
            FinancialEvent.GRANT_REVOKED,
            actor=request.user,
            object_type="finance_access_grant",
            object_id=str(grant.external_id),
            clinic=grant.clinic,
        )
        return Response(FinanceAccessGrantSerializer(grant).data)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

_AUTO_GRANULARITY_DAY_SPAN_LIMIT = 45


def _parse_date(params, key, default):
    raw = params.get(key)
    if not raw:
        return default
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({key: "Must be an ISO date (YYYY-MM-DD)."}) from None


class FinanceDashboardView(APIView):
    """
    GET /api/v1/finance/dashboard/
    The original feature request this whole module exists to deliver.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        doctor, grant = resolve_finance_viewer(request.user, request.query_params.get("doctor"))

        today = timezone.localdate()
        date_from = _parse_date(request.query_params, "from", today.replace(day=1))
        date_to = _parse_date(request.query_params, "to", today)
        if date_from > date_to:
            raise ValidationError({"from": "Must not be after `to`."})

        granularity = request.query_params.get("granularity")
        if granularity is None:
            span_days = (date_to - date_from).days + 1
            granularity = "day" if span_days <= _AUTO_GRANULARITY_DAY_SPAN_LIMIT else "month"
        elif granularity not in ("day", "week", "month"):
            raise ValidationError({"granularity": "Must be one of day, week, month."})

        clinic = None
        clinic_id = request.query_params.get("clinic")
        if clinic_id:
            from apps.clinics.models import Clinic

            clinic = get_object_or_404(Clinic, external_id=clinic_id, deleted=False)

        business_unit = None
        unit_id = request.query_params.get("business_unit")
        if unit_id:
            business_unit = get_object_or_404(BusinessUnit, external_id=unit_id, deleted=False)

        data = compute_dashboard(
            doctor,
            grant,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            clinic=clinic,
            business_unit=business_unit,
            source_type=request.query_params.get("source_type"),
        )
        return Response(data)
