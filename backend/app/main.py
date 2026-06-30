"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_config import setup_logging
from app.routes import hardening, servers
from app.services.ssh_service import clear_pool

# Configure logging before anything starts emitting records.
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Close any pooled SSH connections on shutdown.
    clear_pool()


app = FastAPI(title="Abstract", version="0.1.0", lifespan=lifespan)

# The Vite dev proxy hides CORS in local dev, but proper CORS is correct for any
# direct calls and for prod where the frontend and backend may be on different
# subdomains. Origins come from the same allowlist used to verify Clerk tokens.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.clerk_authorized_parties,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(servers.router)
app.include_router(hardening.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}
