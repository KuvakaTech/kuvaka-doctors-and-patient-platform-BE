from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import HealthCheckView

urlpatterns = [
    # Public health check — no auth, accessible to anyone (PMs, ops, load balancers)
    path("health/", HealthCheckView.as_view(), name="health-check"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("config.api_router")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
