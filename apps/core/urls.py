from django.urls import path

from apps.core.views import (
    BreakGlassListView,
    BreakGlassReviewView,
    BreakGlassView,
    FinancialAuditLogListView,
    HealthCheckView,
)

urlpatterns = [
    path("health/", HealthCheckView.as_view(), name="health-check"),
    # Break-glass — HIPAA § 164.312(a)(2)(ii)
    path("break-glass/", BreakGlassView.as_view(), name="break-glass-invoke"),
    path("break-glass/list/", BreakGlassListView.as_view(), name="break-glass-list"),
    path(
        "break-glass/<int:event_id>/review/",
        BreakGlassReviewView.as_view(),
        name="break-glass-review",
    ),
    # Financial audit review
    path("financial-audit/", FinancialAuditLogListView.as_view(), name="financial-audit-list"),
]
