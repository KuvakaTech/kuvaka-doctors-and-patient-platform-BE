from django.urls import path

from apps.billing.views import (
    ApplyAdvanceView,
    ChargeItemCancelView,
    ChargeItemDefinitionDetailView,
    ChargeItemDefinitionListCreateView,
    ChargeItemListCreateView,
    InvoiceCancelView,
    InvoiceDetailView,
    InvoiceIssueView,
    InvoiceListCreateView,
    MyAccountsView,
    MyInvoicesView,
    MyPaymentsView,
    PatientAccountDetailView,
    PatientAccountListView,
    PaymentListCreateView,
    ReconciliationView,
)

_clinic_prefix = "clinics/<uuid:clinic_external_id>"
_account_prefix = f"{_clinic_prefix}/accounts/<uuid:account_external_id>"

urlpatterns = [
    path(
        f"{_clinic_prefix}/definitions/",
        ChargeItemDefinitionListCreateView.as_view(),
        name="billing-definition-list-create",
    ),
    path(
        f"{_clinic_prefix}/definitions/<uuid:external_id>/",
        ChargeItemDefinitionDetailView.as_view(),
        name="billing-definition-detail",
    ),
    path(
        f"{_clinic_prefix}/accounts/",
        PatientAccountListView.as_view(),
        name="billing-account-list",
    ),
    path(
        f"{_clinic_prefix}/accounts/<uuid:external_id>/",
        PatientAccountDetailView.as_view(),
        name="billing-account-detail",
    ),
    path(
        f"{_account_prefix}/charge-items/",
        ChargeItemListCreateView.as_view(),
        name="billing-charge-item-list-create",
    ),
    path(
        f"{_account_prefix}/charge-items/<uuid:external_id>/cancel/",
        ChargeItemCancelView.as_view(),
        name="billing-charge-item-cancel",
    ),
    path(
        f"{_account_prefix}/invoices/",
        InvoiceListCreateView.as_view(),
        name="billing-invoice-list-create",
    ),
    path(
        f"{_account_prefix}/invoices/<uuid:external_id>/",
        InvoiceDetailView.as_view(),
        name="billing-invoice-detail",
    ),
    path(
        f"{_account_prefix}/invoices/<uuid:external_id>/issue/",
        InvoiceIssueView.as_view(),
        name="billing-invoice-issue",
    ),
    path(
        f"{_account_prefix}/invoices/<uuid:external_id>/cancel/",
        InvoiceCancelView.as_view(),
        name="billing-invoice-cancel",
    ),
    path(
        f"{_account_prefix}/payments/",
        PaymentListCreateView.as_view(),
        name="billing-payment-list-create",
    ),
    path(
        f"{_account_prefix}/apply-advance/",
        ApplyAdvanceView.as_view(),
        name="billing-apply-advance",
    ),
    path(
        f"{_clinic_prefix}/reconciliation/",
        ReconciliationView.as_view(),
        name="billing-reconciliation",
    ),
    path("my/accounts/", MyAccountsView.as_view(), name="billing-my-accounts"),
    path("my/invoices/", MyInvoicesView.as_view(), name="billing-my-invoices"),
    path("my/payments/", MyPaymentsView.as_view(), name="billing-my-payments"),
]
