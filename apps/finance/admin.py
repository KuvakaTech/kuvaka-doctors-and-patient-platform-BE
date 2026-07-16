from django.contrib import admin

from apps.finance.models import BusinessUnit, FinanceAccessGrant, RevenueEntry, RevenueShareRule

admin.site.register(BusinessUnit)
admin.site.register(RevenueShareRule)
admin.site.register(RevenueEntry)
admin.site.register(FinanceAccessGrant)
