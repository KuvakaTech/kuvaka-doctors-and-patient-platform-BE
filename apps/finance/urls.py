from django.urls import path

from apps.finance.views import (
    BusinessUnitDetailView,
    BusinessUnitListCreateView,
    FinanceAccessGrantListCreateView,
    FinanceAccessGrantRevokeView,
    FinanceDashboardView,
    RevenueEntryDetailView,
    RevenueEntryListCreateView,
    RevenueShareRuleDetailView,
    RevenueShareRuleListCreateView,
)

urlpatterns = [
    path(
        "business-units/",
        BusinessUnitListCreateView.as_view(),
        name="finance-business-unit-list-create",
    ),
    path(
        "business-units/<uuid:external_id>/",
        BusinessUnitDetailView.as_view(),
        name="finance-business-unit-detail",
    ),
    path("entries/", RevenueEntryListCreateView.as_view(), name="finance-entry-list-create"),
    path(
        "entries/<uuid:external_id>/",
        RevenueEntryDetailView.as_view(),
        name="finance-entry-detail",
    ),
    path(
        "share-rules/",
        RevenueShareRuleListCreateView.as_view(),
        name="finance-share-rule-list-create",
    ),
    path(
        "share-rules/<uuid:external_id>/",
        RevenueShareRuleDetailView.as_view(),
        name="finance-share-rule-detail",
    ),
    path(
        "access-grants/",
        FinanceAccessGrantListCreateView.as_view(),
        name="finance-access-grant-list-create",
    ),
    path(
        "access-grants/<uuid:external_id>/revoke/",
        FinanceAccessGrantRevokeView.as_view(),
        name="finance-access-grant-revoke",
    ),
    path("dashboard/", FinanceDashboardView.as_view(), name="finance-dashboard"),
]
