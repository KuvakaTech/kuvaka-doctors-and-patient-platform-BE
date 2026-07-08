from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthCheckView(APIView):
    """Unauthenticated liveness probe for load balancers/orchestrators."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"})
