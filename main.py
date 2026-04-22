"""
main.py — FastAPI application entrypoint for netorch v1.2.0.
"""
from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api.auth import require_auth
from api.middleware import limiter
from api.routes.jobs      import router as jobs_router
from api.routes.logs      import router as logs_router
from api.routes.devices   import router as devices_router
from api.routes.inventory import router as inventory_router
from api.routes.runbooks  import router as runbooks_router
from core.config import server
from core.executor import active_job_count
from core.config import executor as exec_cfg
from api.schemas import SystemStatsResponse

app = FastAPI(
    title="netorch — Network Configuration Orchestrator",
    description=(
        "Lightweight SSH-based audit and remediation orchestrator "
        "for Cisco IOS/IOS-XE, IOS-XR, and Linux devices."
    ),
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda req, exc: JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    ),
)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authenticated routers
app.include_router(jobs_router,      dependencies=[Depends(require_auth)])
app.include_router(logs_router,      dependencies=[Depends(require_auth)])
app.include_router(devices_router,   dependencies=[Depends(require_auth)])
app.include_router(inventory_router, dependencies=[Depends(require_auth)])
app.include_router(runbooks_router,  dependencies=[Depends(require_auth)])


@app.get("/health", tags=["system"], summary="Health check — no auth required")
def health():
    return {"status": "ok", "service": "netorch", "version": "1.2.0"}


@app.get(
    "/stats",
    tags=["system"],
    response_model=SystemStatsResponse,
    summary="Live executor stats — no auth required",
)
def stats():
    return SystemStatsResponse(
        active_jobs     = active_job_count(),
        max_queue_depth = exec_cfg.max_queue_depth,
        version         = "1.2.0",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=server.host,
        port=server.port,
        reload=False,
        workers=1,
    )
