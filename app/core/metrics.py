# Prometheus metrics for FastAPI
from prometheus_client import Counter, Histogram, generate_latest
from fastapi import Response

# Custom business metrics
api_requests_total = Counter(
    'api_requests_total',
    'Total API requests',
    ['method', 'endpoint', 'status']
)

api_response_time = Histogram(
    'api_response_time_seconds',
    'API response time in seconds',
    ['method', 'endpoint']
)

def metrics_endpoint():
    """Generate Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type="text/plain")
