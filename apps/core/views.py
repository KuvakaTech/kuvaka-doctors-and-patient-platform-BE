import time

from django.conf import settings
from django.db import connection
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import EmergencyAccess, FinancialAuditLog
from apps.core.serializers import FinancialAuditLogSerializer
from apps.core.services.break_glass import invoke_break_glass, review_break_glass
from apps.users.models import User, UserType


class IsStaffUser(BasePermission):
    """
    Grants access only to users with is_staff=True (CLINIC_ADMIN).
    Tighter than DRF's IsAdminUser — same semantics but named clearly.
    """

    message = "Break-glass access is restricted to clinic administrators."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


class HealthCheckView(APIView):
    """
    Public health check endpoint — no auth required.
    Checks DB connectivity and reports status of each service so anyone
    (PMs, ops, on-call) can verify the platform is up at a glance.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        overall = "ok"

        # --- Database ---
        t0 = time.monotonic()
        try:
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            checks["database"] = {
                "status": "ok",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        except Exception as exc:
            checks["database"] = {"status": "error", "detail": str(exc)}
            overall = "degraded"

        # --- Cache / Redis (optional — only checked if CACHES is configured) ---
        try:
            from django.core.cache import cache

            t0 = time.monotonic()
            cache.set("healthcheck_probe", "1", timeout=5)
            val = cache.get("healthcheck_probe")
            if val == "1":
                checks["cache"] = {
                    "status": "ok",
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                }
            else:
                checks["cache"] = {"status": "error", "detail": "probe value mismatch"}
                overall = "degraded"
        except Exception as exc:
            # Redis not available in all envs — warn but don't fail overall
            checks["cache"] = {"status": "unavailable", "detail": str(exc)}

        # --- Email (Brevo) — config check only, no actual send ---
        brevo_configured = bool(getattr(settings, "BREVO_API_KEY", ""))
        checks["email"] = {
            "status": "ok" if brevo_configured else "unconfigured",
            "provider": "Brevo",
        }

        http_status = status.HTTP_200_OK if overall == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE

        return Response(
            {
                "status": overall,
                "checks": checks,
                "version": getattr(settings, "APP_VERSION", "0.1.0"),
            },
            status=http_status,
        )


# ---------------------------------------------------------------------------
# Break-glass (emergency access) — HIPAA § 164.312(a)(2)(ii)
# ---------------------------------------------------------------------------


class BreakGlassView(APIView):
    """
    Invoke emergency break-glass access to a specific patient record.

    Requires:
      - Authenticated request with is_staff=True (CLINIC_ADMIN)
      - patient_id: the external_id (UUID) of the patient to access
      - justification: free-text reason, stored permanently

    Returns the EmergencyAccess record ID so the caller can reference it
    in subsequent audit review. The actual patient data is not returned
    here — this endpoint purely authorises and records the access. The
    caller is expected to fetch the patient record via the normal patient
    profile API immediately after.
    """

    permission_classes = [IsAuthenticated, IsStaffUser]

    def post(self, request):
        patient_id = request.data.get("patient_id", "")
        justification = request.data.get("justification", "").strip()

        if not patient_id:
            return Response(
                {"detail": "patient_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not justification:
            return Response(
                {"detail": "justification is required. Describe why emergency access is needed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(justification) < 20:
            return Response(
                {"detail": "justification must be at least 20 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        patient = User.objects.filter(external_id=patient_id, user_type=UserType.PATIENT).first()
        if patient is None:
            return Response(
                {"detail": "Patient not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            event = invoke_break_glass(
                request,
                admin_user=request.user,
                patient_user=patient,
                justification=justification,
            )
        except (PermissionError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                "detail": "Emergency access granted and recorded.",
                "emergency_access_id": event.pk,
                "patient_external_id": str(patient.external_id),
                "accessed_at": event.accessed_at,
                "warning": (
                    "This access has been permanently logged and will be reviewed "
                    "by a clinic administrator."
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class BreakGlassListView(APIView):
    """
    List all break-glass events. Staff-only.
    Supports ?unreviewed=true to filter to events pending review.
    """

    permission_classes = [IsAuthenticated, IsStaffUser]

    def get(self, request):
        qs = EmergencyAccess.objects.select_related(
            "accessed_by", "patient", "reviewed_by"
        ).order_by("-accessed_at")

        if request.query_params.get("unreviewed") == "true":
            qs = qs.filter(reviewed_at__isnull=True)

        data = [
            {
                "id": ev.pk,
                "accessed_by": ev.accessed_by.email if ev.accessed_by else None,
                "patient": ev.patient.email if ev.patient else None,
                "justification": ev.justification,
                "ip_address": ev.ip_address,
                "accessed_at": ev.accessed_at,
                "is_reviewed": ev.is_reviewed,
                "reviewed_by": ev.reviewed_by.email if ev.reviewed_by else None,
                "reviewed_at": ev.reviewed_at,
                "review_notes": ev.review_notes,
            }
            for ev in qs
        ]
        return Response(data)


class BreakGlassReviewView(APIView):
    """
    Mark a break-glass event as reviewed.

    Requires:
      - is_staff=True
      - reviewer must differ from the admin who invoked the access
      - optional review_notes
    """

    permission_classes = [IsAuthenticated, IsStaffUser]

    def post(self, request, event_id: int):
        event = EmergencyAccess.objects.filter(pk=event_id).first()
        if event is None:
            return Response(
                {"detail": "Emergency access record not found."}, status=status.HTTP_404_NOT_FOUND
            )

        review_notes = request.data.get("review_notes", "").strip()

        try:
            review_break_glass(
                event=event,
                reviewer=request.user,
                review_notes=review_notes,
            )
        except (PermissionError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "detail": "Break-glass event marked as reviewed.",
                "emergency_access_id": event.pk,
                "reviewed_at": event.reviewed_at,
            }
        )


# ---------------------------------------------------------------------------
# Financial audit review
# ---------------------------------------------------------------------------


class FinancialAuditLogListView(generics.ListAPIView):
    """
    Read-only trail over every money-mutating action in apps.finance and
    apps.billing.

    `?clinic=<external_id>` — requires a clinic admin/doctor role at that
    clinic; returns every event scoped to it.
    Omitted entirely -> the safe default of "your own actions only"
    (never privileged, always allowed — matches AuditLog's own review
    surface convention for a caller who isn't a clinic admin anywhere).

    Optional filters: `?event=`, `?object_type=`, `?from=`, `?to=`
    (on `created_at`, inclusive date bounds).
    """

    serializer_class = FinancialAuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = FinancialAuditLog.objects.select_related("actor", "clinic").order_by("-created_at")

        clinic_external_id = self.request.query_params.get("clinic")
        if clinic_external_id:
            # Local import — apps.core sits below apps.clinics in
            # LOCAL_APPS, so importing apps.clinics back at module load
            # time here would be circular.
            from apps.clinics.models import Clinic
            from apps.clinics.permissions import require_admin

            clinic = get_object_or_404(Clinic, external_id=clinic_external_id, deleted=False)
            require_admin(self.request.user, clinic)
            qs = qs.filter(clinic=clinic)
        else:
            qs = qs.filter(actor=self.request.user)

        params = self.request.query_params
        if params.get("event"):
            qs = qs.filter(event=params["event"])
        if params.get("object_type"):
            qs = qs.filter(object_type=params["object_type"])
        if params.get("from"):
            qs = qs.filter(created_at__date__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(created_at__date__lte=params["to"])
        return qs
