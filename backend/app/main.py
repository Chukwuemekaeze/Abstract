"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_config import logger, setup_logging
from app.middleware.logging import RequestLoggingMiddleware
from app.routes import (
    account,
    env_files,
    hardening,
    project_runtime,
    projects,
    servers,
)
from app.services.ssh_service import clear_pool

# Configure logging before anything starts emitting records.
_settings = get_settings()
setup_logging(level=_settings.log_level, fmt=_settings.log_format)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup complete")
    yield
    # Close any pooled SSH connections on shutdown.
    clear_pool()
    logger.info("Application shutdown: cleared SSH connection pool")


app = FastAPI(title="Abstract", version="0.1.0", lifespan=lifespan)

# The Vite dev proxy hides CORS in local dev, but proper CORS is correct for any
# direct calls and for prod where the frontend and backend may be on different
# subdomains. Origins come from the same allowlist used to verify Clerk tokens.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.clerk_authorized_parties,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added last so it sits outermost: it assigns the request id and logs in/out around
# everything else, including CORS handling.
app.add_middleware(RequestLoggingMiddleware)

app.include_router(servers.router)
app.include_router(hardening.router)
app.include_router(projects.router)
app.include_router(env_files.router)
app.include_router(project_runtime.router)
app.include_router(account.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}
