from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.clinics.views import (
    ClinicInventoryDetailView,
    ClinicInventoryListCreateView,
    ClinicStaffDetailView,
    ClinicStaffListCreateView,
    ClinicViewSet,
    MedicineListCreateView,
    PurchaseOrderListCreateView,
    PurchaseOrderReceiveView,
    StaffTaskGrantListCreateView,
    StaffTaskGrantRevokeView,
)

router = DefaultRouter()
router.register("", ClinicViewSet, basename="clinic")

urlpatterns = [
    path("medicines/", MedicineListCreateView.as_view(), name="medicine-list-create"),
    path(
        "<uuid:clinic_external_id>/staff/",
        ClinicStaffListCreateView.as_view(),
        name="clinic-staff-list-create",
    ),
    path(
        "<uuid:clinic_external_id>/staff/<uuid:membership_external_id>/",
        ClinicStaffDetailView.as_view(),
        name="clinic-staff-detail",
    ),
    path(
        "<uuid:clinic_external_id>/task-grants/",
        StaffTaskGrantListCreateView.as_view(),
        name="clinic-task-grant-list-create",
    ),
    path(
        "<uuid:clinic_external_id>/task-grants/<uuid:external_id>/revoke/",
        StaffTaskGrantRevokeView.as_view(),
        name="clinic-task-grant-revoke",
    ),
    path(
        "<uuid:clinic_external_id>/inventory/",
        ClinicInventoryListCreateView.as_view(),
        name="clinic-inventory-list-create",
    ),
    path(
        "<uuid:clinic_external_id>/inventory/<uuid:external_id>/",
        ClinicInventoryDetailView.as_view(),
        name="clinic-inventory-detail",
    ),
    path(
        "<uuid:clinic_external_id>/purchase-orders/",
        PurchaseOrderListCreateView.as_view(),
        name="clinic-purchase-order-list-create",
    ),
    path(
        "<uuid:clinic_external_id>/purchase-orders/<uuid:external_id>/receive/",
        PurchaseOrderReceiveView.as_view(),
        name="clinic-purchase-order-receive",
    ),
    *router.urls,
]
