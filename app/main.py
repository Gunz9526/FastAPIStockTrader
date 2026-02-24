import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.metrics import api_requests_total, api_response_time, metrics_endpoint
from app.middleware.rate_limit import setup_rate_limiting

# Setup Logging
setup_logging()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Init DB, connection pools, etc.
    print("Application Startup")
    yield
    # Shutdown: Close connections
    print("Application Shutdown")

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="2.0.0",
    lifespan=lifespan
)

# Setup rate limiting
setup_rate_limiting(app)

# Metrics middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    method = request.method
    endpoint = request.url.path

    # Extract client IP (handles X-Forwarded-For for proxies)
    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    # Log incoming request with IP
    logger.info(
        f"[{method}] {endpoint} - IP: {client_ip}",
        extra={
            "client_ip": client_ip,
            "method": method,
            "endpoint": endpoint,
            "user_agent": request.headers.get("User-Agent", "unknown")
        }
    )

    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    # Record metrics
    api_requests_total.labels(method=method, endpoint=endpoint, status=response.status_code).inc()
    api_response_time.labels(method=method, endpoint=endpoint).observe(duration)

    return response

# CORS Middleware
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include API Router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Metrics endpoint
@app.get("/metrics")
async def metrics():
    return metrics_endpoint()

@app.get("/health")
async def health_check(request: Request):
    return {"status": "healthy"}
